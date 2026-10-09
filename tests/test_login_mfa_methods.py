import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import Response

from app import schemas
from app.api.endpoints import login as login_endpoint
from app.api.endpoints import mfa as mfa_endpoint
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
        "success": False,
        "message": "需要二次验证",
        "data": {"mfa_methods": ["otp"]},
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
    assert response.message == ""
    assert not hasattr(response, "message_i18n")


def test_passkey_authentication_start_returns_object_options(monkeypatch):
    """Passkey 认证选项应作为对象返回，避免统一响应模型校验失败。"""
    monkeypatch.setattr(
        mfa_endpoint.PassKeyHelper,
        "generate_authentication_options",
        staticmethod(
            lambda **_: ('{"challenge":"auth-challenge","timeout":60000}', "challenge")
        ),
    )
    monkeypatch.setattr(
        mfa_endpoint.PasskeyChallengeStore,
        "issue",
        staticmethod(lambda **_: "authentication-transaction"),
    )

    response = mfa_endpoint.passkey_authenticate_start(
        mfa_endpoint.PassKeyAuthenticationStart()
    )
    payload = schemas.PasskeyStartData.model_validate(response.data)

    assert response.success is True
    assert payload.options.root["challenge"] == "auth-challenge"
    assert payload.transaction_token == "authentication-transaction"


def test_passkey_registration_start_returns_object_options(monkeypatch):
    """Passkey 注册选项应作为对象返回，避免统一响应模型校验失败。"""
    monkeypatch.setattr(
        mfa_endpoint.PassKeyHelper,
        "generate_registration_options",
        staticmethod(
            lambda **_: ('{"challenge":"register-challenge"}', "challenge")
        ),
    )
    monkeypatch.setattr(
        mfa_endpoint.PasskeyChallengeStore,
        "issue",
        staticmethod(lambda **_: "registration-transaction"),
    )
    user = SimpleNamespace(id=1, name="user", settings={})

    response = mfa_endpoint.passkey_register_start(
        current_user=user,
        service=SimpleNamespace(list_by_user_id=lambda user_id: []),
    )
    payload = schemas.PasskeyStartData.model_validate(response.data)

    assert response.success is True
    assert payload.options.root["challenge"] == "register-challenge"
    assert payload.transaction_token == "registration-transaction"


class _FakeThrottle:
    """记录调用并按预设返回等待秒数的登录限流器替身。"""

    def __init__(self, retry_after: int = 0) -> None:
        self._retry_after = retry_after
        self.failures: list[tuple[str, str]] = []
        self.successes: list[tuple[str, str]] = []

    def retry_after(self, key):
        """返回预设等待秒数。"""
        return self._retry_after

    def record_failure(self, key):
        """记录失败键。"""
        self.failures.append(key)
        return 0

    def record_success(self, key):
        """记录成功键。"""
        self.successes.append(key)


def test_login_rejects_with_429_while_throttled(monkeypatch):
    """锁定期内直接返回 429 与 Retry-After，不触发凭据校验。"""
    throttle = _FakeThrottle(retry_after=42)
    monkeypatch.setattr(login_endpoint, "get_login_throttle", lambda: throttle)
    calls = []

    class FakeUserChain:
        """记录是否被调用。"""

        def user_authenticate(self, **kwargs):
            """不应被调用。"""
            calls.append(kwargs)
            return False, "unexpected"

    monkeypatch.setattr(login_endpoint, "UserChain", FakeUserChain)

    with pytest.raises(HTTPException) as exc_info:
        login_endpoint.login_access_token(
            request=_request(),
            response=Response(),
            form_data=_form(),
        )

    assert exc_info.value.status_code == 429
    assert exc_info.value.headers == {"Retry-After": "42"}
    assert calls == []
    assert throttle.failures == []


def test_login_failure_records_throttle_key(monkeypatch):
    """密码错误按 (客户端地址, 小写用户名) 记一次失败。"""
    throttle = _FakeThrottle()
    monkeypatch.setattr(login_endpoint, "get_login_throttle", lambda: throttle)

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
            form_data=SimpleNamespace(username=" User ", password="password"),
        )

    assert exc_info.value.status_code == 401
    assert throttle.failures == [("testclient", "user")]
    assert throttle.successes == []


def test_login_mfa_challenge_does_not_count_as_failure(monkeypatch):
    """密码正确但需二次验证时不计入失败。"""
    throttle = _FakeThrottle()
    monkeypatch.setattr(login_endpoint, "get_login_throttle", lambda: throttle)

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
    assert throttle.failures == []
    assert throttle.successes == []


def test_login_success_clears_throttle(monkeypatch):
    """登录成功后清除该键的失败记录。"""
    throttle = _FakeThrottle()
    monkeypatch.setattr(login_endpoint, "get_login_throttle", lambda: throttle)
    user = SimpleNamespace(
        id=1, name="user", is_superuser=True, avatar=None, permissions={}
    )

    class FakeUserChain:
        """返回认证成功的用户。"""

        def user_authenticate(self, username, password, mfa_code=None):
            """模拟认证成功。"""
            return True, user

    monkeypatch.setattr(login_endpoint, "UserChain", FakeUserChain)
    monkeypatch.setattr(
        login_endpoint, "SitesHelper", lambda: SimpleNamespace(auth_level=2)
    )
    monkeypatch.setattr(login_endpoint, "create_access_token", lambda **_: "token")
    monkeypatch.setattr(
        login_endpoint, "set_or_refresh_resource_token_cookie", lambda *_args, **_kwargs: None
    )

    token = login_endpoint.login_access_token(
        request=_request(),
        response=Response(),
        form_data=_form(),
    )

    assert token.access_token == "token"
    assert throttle.successes == [("testclient", "user")]
    assert throttle.failures == []
