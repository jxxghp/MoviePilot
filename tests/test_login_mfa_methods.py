import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import Response

from app.api.endpoints import login as login_endpoint
from app.chain.user import MfaRequired, UserChain


def _request() -> Request:
    """构造登录接口所需的最小请求。"""
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/login/access-token",
            "headers": [(b"host", b"testserver")],
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("testclient", 123),
        }
    )


def _form() -> SimpleNamespace:
    """构造密码登录表单契约。"""
    return SimpleNamespace(username="user", password="password")


def test_verify_mfa_requires_otp_when_enabled():
    """密码通过后应返回账号已启用的 OTP 二次验证方式。"""
    user = SimpleNamespace(id=1, name="user", is_otp=True, otp_secret="")

    result = UserChain._verify_mfa(user=user, mfa_code=None)

    assert isinstance(result, MfaRequired)
    assert result.methods == ("otp",)


def test_verify_mfa_ignores_passkeys_when_otp_is_disabled():
    """Passkey 独立登录能力不应改变密码登录结果。"""
    user = SimpleNamespace(id=1, name="user", is_otp=False, otp_secret="")

    assert UserChain._verify_mfa(user=user, mfa_code=None) is True


def test_login_mfa_response_contains_methods_after_password_verification(monkeypatch):
    """MFA 响应应保持旧标记并补充结构化方法列表。"""

    class FakeUserChain:
        """返回已通过密码校验的 MFA 要求。"""

        def user_authenticate(self, username, password, mfa_code=None):
            """模拟账号启用了 OTP。"""
            return False, MfaRequired(methods=("otp",))

    monkeypatch.setattr(login_endpoint, "UserChain", FakeUserChain)

    response = login_endpoint.login_access_token(
        request=_request(),
        response=Response(),
        form_data=_form(),
    )

    assert response.status_code == 401
    assert response.headers["x-mfa-required"] == "true"
    assert json.loads(response.body) == {
        "detail": "需要二次验证",
        "mfa_methods": ["otp"],
    }


def test_login_invalid_password_does_not_expose_mfa_methods(monkeypatch):
    """密码未通过时不得返回账号的 MFA 能力。"""

    class FakeUserChain:
        """返回普通认证失败。"""

        def user_authenticate(self, username, password, mfa_code=None):
            """模拟错误密码。"""
            return False, "用户名、密码或验证码错误"

    monkeypatch.setattr(login_endpoint, "UserChain", FakeUserChain)

    with pytest.raises(HTTPException) as exc_info:
        login_endpoint.login_access_token(
            request=_request(),
            response=Response(),
            form_data=_form(),
        )

    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "用户名或密码错误"
    assert "X-MFA-Required" not in (exc_info.value.headers or {})


def test_wallpaper_returns_url_in_data(monkeypatch):
    """登录壁纸地址应放入 data，message 只保留消息文本。"""

    class FakeWallpaperHelper:
        """返回固定登录壁纸地址。"""

        def get_wallpaper(self):
            """返回测试壁纸地址。"""
            return "https://images.example/wallpaper.jpg"

    monkeypatch.setattr(login_endpoint, "WallpaperHelper", FakeWallpaperHelper)

    response = login_endpoint.wallpaper()

    assert response.success is True
    assert response.data == "https://images.example/wallpaper.jpg"
    assert response.message is None
    assert response.message_i18n is None
