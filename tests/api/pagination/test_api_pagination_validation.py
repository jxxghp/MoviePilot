"""
验证消息、会话、下载、整理与订阅历史分页接口的正整数参数校验契约。
"""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException, Response
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient

from app.adapters.web.security.access import verify_token
from app.api.dependencies.agent import get_agent_chat_service, get_message_query_service
from app.api.dependencies.auth import get_current_active_user, get_current_active_user_async
from app.api.dependencies.history import get_history_query_service
from app.api.dependencies.subscription import get_subscription_query_service
from app.api.endpoints import agent, history, message, subscribe
from app.api.response import ResponseAPIRoute
from app.application.messaging.message import MessageViewer
from app.factory import (
    localized_http_exception_handler,
    localized_unhandled_exception_handler,
    localized_validation_exception_handler,
)
from app.schemas.token import TokenPayload


def _build_pagination_test_app(*, fail_on_service_call: bool) -> tuple[FastAPI, dict[str, Any]]:
    """
    构造挂载受测分页路由与统一异常处理器的隔离测试应用。

    :param fail_on_service_call: 为真时底层查询桩一旦被调用即抛出异常，用于验证非法参数不会下沉到服务与 SQL 层
    :return: 测试 FastAPI 实例与注入的查询服务桩映射
    """
    app = FastAPI()
    app.router.route_class = ResponseAPIRoute
    app.add_exception_handler(HTTPException, localized_http_exception_handler)
    app.add_exception_handler(RequestValidationError, localized_validation_exception_handler)
    app.add_exception_handler(Exception, localized_unhandled_exception_handler)

    app.include_router(message.router, prefix="/api/v1/message")
    app.include_router(agent.router, prefix="/api/v1/message/agent")
    app.include_router(history.router, prefix="/api/v1/history")
    app.include_router(subscribe.router, prefix="/api/v1/subscribe")

    principal = SimpleNamespace(
        id=1,
        name="admin",
        is_superuser=True,
        notification_identity="user:1",
    )
    token_payload = TokenPayload(sub="1", username="admin", super_user=True)

    if fail_on_service_call:
        guard_error = RuntimeError("非法分页参数不得下沉到查询服务或数据库 SQL 层")
        message_service = SimpleNamespace(
            list_history=AsyncMock(side_effect=guard_error),
            list_notifications=AsyncMock(side_effect=guard_error),
        )
        agent_chat_service = SimpleNamespace(
            list=AsyncMock(side_effect=guard_error),
        )
        history_service = SimpleNamespace(
            list_download=AsyncMock(side_effect=guard_error),
            count_download=AsyncMock(side_effect=guard_error),
            list_transfer=AsyncMock(side_effect=guard_error),
        )
        subscription_service = SimpleNamespace(
            list_history=AsyncMock(side_effect=guard_error),
            count_history=AsyncMock(side_effect=guard_error),
        )
    else:
        message_service = SimpleNamespace(
            list_history=AsyncMock(return_value=[]),
            list_notifications=AsyncMock(return_value=[]),
        )
        agent_chat_service = SimpleNamespace(
            list=AsyncMock(return_value=[]),
        )
        history_service = SimpleNamespace(
            list_download=AsyncMock(return_value=[]),
            count_download=AsyncMock(return_value=0),
            list_transfer=AsyncMock(return_value={"list": [], "total": 0}),
        )
        subscription_service = SimpleNamespace(
            list_history=AsyncMock(return_value=[]),
            count_history=AsyncMock(return_value=0),
        )

    app.dependency_overrides[verify_token] = lambda: token_payload
    app.dependency_overrides[get_current_active_user] = lambda: principal
    app.dependency_overrides[get_current_active_user_async] = lambda: principal
    app.dependency_overrides[get_message_query_service] = lambda: message_service
    app.dependency_overrides[get_agent_chat_service] = lambda: agent_chat_service
    app.dependency_overrides[get_history_query_service] = lambda: history_service
    app.dependency_overrides[get_subscription_query_service] = lambda: subscription_service

    stubs = {
        "message": message_service,
        "agent_chat": agent_chat_service,
        "history": history_service,
        "subscription": subscription_service,
    }
    return app, stubs


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/message/notification",
        "/api/v1/message/history",
        "/api/v1/message/web",
        "/api/v1/message/agent/sessions",
        "/api/v1/history/download",
        "/api/v1/history/transfer",
        "/api/v1/subscribe/history/电影",
    ],
)
@pytest.mark.parametrize(
    ("param_name", "invalid_value"),
    [
        ("count", -1),
        ("count", 0),
        ("page", -1),
        ("page", 0),
    ],
)
def test_database_paginated_endpoints_reject_non_positive_pagination_with_422(
    path: str,
    param_name: str,
    invalid_value: int,
) -> None:
    """
    验证数据库分页接口对负数和零页码或条数返回 422 参数校验错误，避免下沉触发 500。

    :param path: 待验证的分页接口路径
    :param param_name: 分页查询参数名称
    :param invalid_value: 非正整数的非法输入值
    """
    app, stubs = _build_pagination_test_app(fail_on_service_call=True)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get(path, params={param_name: invalid_value})

    assert response.status_code == 422
    payload = response.json()
    assert payload["success"] is False
    assert isinstance(payload["data"], list) and payload["data"]
    assert any(
        issue["location"] == ["query", param_name]
        and issue["error_type"] == "greater_than_equal"
        for issue in payload["data"]
    )
    stubs["message"].list_history.assert_not_awaited()
    stubs["message"].list_notifications.assert_not_awaited()
    stubs["agent_chat"].list.assert_not_awaited()
    stubs["history"].list_download.assert_not_awaited()
    stubs["history"].list_transfer.assert_not_awaited()
    stubs["subscription"].list_history.assert_not_awaited()


def test_database_paginated_endpoints_preserve_defaults_and_large_transfer_page_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    验证省略分页参数时沿用各端点原有默认值，且整理历史继续支持桌面端 1000 条每页窗口。

    :param monkeypatch: 用于隔离通知清理水位读取的测试夹具
    """
    monkeypatch.setattr(
        message,
        "_get_notification_clear_before",
        lambda: SimpleNamespace(all=0, system=0, media=0),
    )
    app, stubs = _build_pagination_test_app(fail_on_service_call=False)
    with TestClient(app) as client:
        assert client.get("/api/v1/message/notification").status_code == 200
        assert client.get("/api/v1/message/history").status_code == 200
        assert client.get("/api/v1/message/agent/sessions").status_code == 200
        assert client.get("/api/v1/history/download").status_code == 200
        assert client.get("/api/v1/history/transfer?page=1&count=1000").status_code == 200
        assert client.get("/api/v1/subscribe/history/电影").status_code == 200

    stubs["message"].list_notifications.assert_awaited_once_with(
        page=1,
        count=20,
        all_clear_before=None,
        system_clear_before=None,
        media_clear_before=None,
        viewer=MessageViewer(super_user=True, userid="1"),
    )
    stubs["message"].list_history.assert_awaited_once_with(
        page=1,
        count=20,
        viewer=MessageViewer(super_user=True, userid="1"),
    )
    stubs["agent_chat"].list.assert_awaited_once()
    assert stubs["agent_chat"].list.await_args.kwargs == {"page": 1, "count": 30}
    stubs["history"].list_download.assert_awaited_once_with(page=1, count=30)
    stubs["history"].list_transfer.assert_awaited_once_with(
        title=None,
        page=1,
        count=1000,
        status=None,
    )
    stubs["subscription"].list_history.assert_awaited_once_with(
        "电影",
        page=1,
        count=30,
        username=None,
    )


def test_direct_calls_with_none_pagination_fall_back_to_endpoint_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    验证直接以 Python 函数方式传入 None 分页值时仍回退为各端点默认正整数。

    :param monkeypatch: 用于隔离通知清理水位读取的测试夹具
    """
    monkeypatch.setattr(
        message,
        "_get_notification_clear_before",
        lambda: SimpleNamespace(all=0, system=0, media=0),
    )
    message_service = SimpleNamespace(list_notifications=AsyncMock(return_value=[]))
    history_service = SimpleNamespace(
        list_download=AsyncMock(return_value=[]),
        count_download=AsyncMock(return_value=0),
    )
    subscription_service = SimpleNamespace(
        list_history=AsyncMock(return_value=[]),
        count_history=AsyncMock(return_value=0),
    )
    principal = SimpleNamespace(
        id=1,
        name="admin",
        is_superuser=True,
        notification_identity="user:1",
    )

    assert asyncio.run(
        message.get_notification_message(
            token_data=TokenPayload(sub=1, username="admin", super_user=True),
            service=message_service,
            page=None,
            count=None,
        )
    ) == []
    assert asyncio.run(
        history.download_history(
            response=Response(),
            page=None,
            count=None,
            query=history_service,
            _=None,
        )
    ) == []
    assert asyncio.run(
        subscribe.subscribe_history(
            mtype="电影",
            response=Response(),
            page=None,
            count=None,
            query=subscription_service,
            current_user=principal,
        )
    ) == []

    message_service.list_notifications.assert_awaited_once_with(
        page=1,
        count=20,
        all_clear_before=None,
        system_clear_before=None,
        media_clear_before=None,
        viewer=MessageViewer(super_user=True, userid="1"),
    )
    history_service.list_download.assert_awaited_once_with(page=1, count=30)
    subscription_service.list_history.assert_awaited_once_with(
        "电影",
        page=1,
        count=30,
        username=None,
    )


def test_openapi_and_mcp_schema_declare_minimum_one_for_history_pagination() -> None:
    """
    验证 OpenAPI 与外部 MCP 输入合同为分页参数声明 minimum=1 且保留端点默认值。
    """
    app, _ = _build_pagination_test_app(fail_on_service_call=False)
    openapi = app.openapi()
    expected_defaults = {
        "/api/v1/message/notification": (1, 20),
        "/api/v1/message/history": (1, 20),
        "/api/v1/message/web": (1, 20),
        "/api/v1/message/agent/sessions": (1, 30),
        "/api/v1/history/download": (1, 30),
        "/api/v1/history/transfer": (1, 30),
        "/api/v1/subscribe/history/{mtype}": (1, 30),
    }

    for path, (default_page, default_count) in expected_defaults.items():
        parameters = {
            item["name"]: item["schema"]
            for item in openapi["paths"][path]["get"].get("parameters", [])
            if item.get("in") == "query"
        }
        assert "response" not in parameters, path
        for field_name, expected_default in (("page", default_page), ("count", default_count)):
            schema = parameters[field_name]
            assert schema["default"] == expected_default, (path, field_name)
            minimums = [schema.get("minimum")] + [
                branch.get("minimum")
                for branch in schema.get("anyOf", [])
                if isinstance(branch, dict)
            ]
            assert 1 in minimums, (path, field_name, schema)

    from scripts.generate_agent_api_mcp_schema import generate_schema

    stored_schema = json.loads(
        (Path(__file__).parents[3] / "app/agent/policy/resources/api_mcp_schema.json").read_text(
            encoding="utf-8"
        )
    )
    generated_schema = generate_schema()
    stored_branches = {
        item["properties"]["operation_id"]["const"]: item["properties"]["query"]
        for item in stored_schema["oneOf"]
        if item["properties"]["operation_id"].get("const")
        in {"download.history.list", "transfer.history", "subscription.history"}
    }
    generated_branches = {
        item["properties"]["operation_id"]["const"]: item["properties"]["query"]
        for item in generated_schema["oneOf"]
        if item["properties"]["operation_id"].get("const")
        in {"download.history.list", "transfer.history", "subscription.history"}
    }
    assert stored_branches == generated_branches
