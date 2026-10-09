"""CORS 中间件在通配来源与显式来源下的凭据策略契约测试。"""

import pytest
from fastapi.testclient import TestClient

from app.factory import create_app, resolve_cors_policy
from app.runtime.config import settings

LOGIN_PATH = "/api/v1/login/access-token"


def _preflight(client: TestClient, origin: str):
    """发送一次登录接口的 CORS 预检请求。"""
    return client.options(
        LOGIN_PATH,
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )


@pytest.mark.parametrize(
    ("allowed_hosts", "expected"),
    [
        (None, (["*"], False)),
        ([], (["*"], False)),
        (["*"], (["*"], False)),
        (["*", "https://mp.example"], (["*"], False)),
        (["https://mp.example"], (["https://mp.example"], True)),
        ([" https://mp.example ", "", "https://b.example"], (["https://mp.example", "https://b.example"], True)),
    ],
)
def test_resolve_cors_policy_disables_credentials_for_wildcard(allowed_hosts, expected):
    """通配来源一律关闭凭据，显式来源才允许携带凭据。"""
    assert resolve_cors_policy(allowed_hosts) == expected


def test_wildcard_origins_do_not_allow_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认通配配置下预检返回 *，且不声明允许凭据。"""
    monkeypatch.setattr(settings, "ALLOWED_HOSTS", ["*"])
    client = TestClient(create_app())

    response = _preflight(client, "https://evil.example")

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "*"
    assert "access-control-allow-credentials" not in response.headers


def test_explicit_origins_echo_origin_with_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """显式来源命中时回显 Origin 并允许携带凭据，未列出的来源不返回 CORS 头。"""
    monkeypatch.setattr(settings, "ALLOWED_HOSTS", ["https://mp.example"])
    client = TestClient(create_app())

    allowed = _preflight(client, "https://mp.example")
    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == "https://mp.example"
    assert allowed.headers["access-control-allow-credentials"] == "true"

    # Starlette 对未列出的来源拒绝预检：400 且不回显 allow-origin，浏览器据此阻止请求
    denied = _preflight(client, "https://evil.example")
    assert denied.status_code == 400
    assert "access-control-allow-origin" not in denied.headers
