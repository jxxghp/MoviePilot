"""
验证订阅全量查询路由真实执行 API_TOKEN 认证后才读取数据。
"""

from collections.abc import Iterator
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.adapters.web.security import access
from app.api.endpoints import subscribe as subscribe_endpoint
from app.schemas.subscribe import Subscribe
from app.schemas.token import TokenPayload


@pytest.fixture
def subscribe_api(monkeypatch) -> Iterator[SimpleNamespace]:
    """
    保留真实认证依赖，仅替换配置、管理员身份端口和订阅查询边界。

    :param monkeypatch: 用例结束后恢复配置读取及身份端口的 pytest 夹具
    :yields: 独立 HTTP 客户端及用于验证读取和身份校验的替身
    """
    monkeypatch.setattr(access, "get_runtime_setting", lambda _key: "test-subscription-api-token")
    identity = Mock(return_value=TokenPayload(sub=7, username="admin", super_user=True))
    monkeypatch.setattr(access, "_superuser_token_payload_provider", identity)
    query = SimpleNamespace(
        count_public=AsyncMock(return_value=2),
        list_public=AsyncMock(return_value=[
            Subscribe(id=1, name="订阅一", username="alice"),
            Subscribe(id=2, name="订阅二", username="bob"),
        ]),
    )
    status = SimpleNamespace(for_subscriptions=AsyncMock(return_value={}))
    app = FastAPI()
    app.include_router(subscribe_endpoint.router, prefix="/api/v1/subscribe")
    app.dependency_overrides[subscribe_endpoint.get_subscription_query_service] = lambda: query
    app.dependency_overrides[subscribe_endpoint.get_subscription_execution_status_service] = lambda: status

    with TestClient(app) as client:
        yield SimpleNamespace(client=client, query=query, status=status, identity=identity)


@pytest.mark.parametrize(
    ("params", "headers"),
    [
        ({}, {}),
        ({"token": ""}, {}),
        ({"token": "wrong-token"}, {}),
        ({}, {"X-API-KEY": "wrong-token"}),
        ({"_": "test-subscription-api-token"}, {}),
        ({"token": "test-subscription-api-token"}, {"X-API-KEY": "wrong-token"}),
    ],
)
def test_subscribe_list_rejects_invalid_credentials_before_reading(subscribe_api, params, headers) -> None:
    """
    缺失或错误凭据必须返回 401，不能查询订阅总量、数据或执行状态。
    """
    response = subscribe_api.client.get("/api/v1/subscribe/list", params=params, headers=headers)

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json() == {"detail": "token 校验不通过"}
    subscribe_api.query.count_public.assert_not_awaited()
    subscribe_api.query.list_public.assert_not_awaited()
    subscribe_api.status.for_subscriptions.assert_not_awaited()
    subscribe_api.identity.assert_not_called()


@pytest.mark.parametrize(
    ("params", "headers"),
    [
        ({"token": "test-subscription-api-token"}, {}),
        ({}, {"X-API-KEY": "test-subscription-api-token"}),
        ({"token": "wrong-token"}, {"X-API-KEY": "test-subscription-api-token"}),
    ],
)
def test_subscribe_list_accepts_valid_credentials_and_preserves_global_query(subscribe_api, params, headers) -> None:
    """
    正确凭据通过当前管理员校验后，仍按既有分页合同读取全部用户订阅。
    """
    response = subscribe_api.client.get(
        "/api/v1/subscribe/list",
        params={**params, "page": 1, "count": 2},
        headers=headers,
    )

    assert response.status_code == 200
    assert response.headers["x-total-count"] == "2"
    assert [(item["id"], item["username"]) for item in response.json()["data"]] == [(1, "alice"), (2, "bob")]
    subscribe_api.query.count_public.assert_awaited_once_with()
    subscribe_api.query.list_public.assert_awaited_once_with(page=1, count=2)
    subscribe_api.status.for_subscriptions.assert_awaited_once_with((1, 2))
    subscribe_api.identity.assert_called_once_with()


def test_subscribe_list_rejects_invalid_admin_identity_before_reading(subscribe_api) -> None:
    """
    正确 API_TOKEN 也不能绕过所绑定管理员的当前身份校验。
    """
    subscribe_api.identity.side_effect = PermissionError("用户不存在或已禁用")

    response = subscribe_api.client.get(
        "/api/v1/subscribe/list", params={"token": "test-subscription-api-token"}
    )

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    subscribe_api.identity.assert_called_once_with()
    subscribe_api.query.count_public.assert_not_awaited()
    subscribe_api.query.list_public.assert_not_awaited()
    subscribe_api.status.for_subscriptions.assert_not_awaited()


def test_subscribe_list_openapi_declares_authentication(subscribe_api) -> None:
    """
    OpenAPI 应声明 API 凭据，不能把认证占位参数暴露为普通查询参数。
    """
    response = subscribe_api.client.get("/openapi.json")

    assert response.status_code == 200
    operation = response.json()["paths"]["/api/v1/subscribe/list"]["get"]
    assert {"api_token_query": []} in operation["security"]
    assert {"api_key_header": []} in operation["security"]
    assert "_" not in {parameter["name"] for parameter in operation.get("parameters", [])}
