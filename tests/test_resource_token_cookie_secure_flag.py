from unittest import TestCase

from fastapi import Response

from app import schemas
from app.core.security import set_or_refresh_resource_token_cookie


class FakeURL:
    def __init__(self, scheme: str) -> None:
        self.scheme = scheme


class FakeRequest:
    """
    最小化的请求桩对象，仅提供 set_or_refresh_resource_token_cookie 所需属性。
    """

    def __init__(self, scheme: str, headers: dict | None = None) -> None:
        self.url = FakeURL(scheme)
        self.headers = headers or {}
        self.cookies: dict = {}


class ResourceTokenCookieSecureFlagTest(TestCase):
    def test_secure_flag_set_when_https_terminated_at_reverse_proxy(self):
        """
        当反向代理（如 nginx）终止 HTTPS 并以 HTTP 转发给后端时，
        资源令牌 Cookie 仍必须携带 secure 属性，不能因为直连请求协议是 http 就降级。
        """
        request = FakeRequest(scheme="http", headers={"x-forwarded-proto": "https"})
        response = Response()
        payload = schemas.TokenPayload(sub=1, username="test", super_user=False, level=1)

        set_or_refresh_resource_token_cookie(request, response, payload)

        set_cookie_header = response.headers.get("set-cookie", "")
        self.assertIn("Secure", set_cookie_header)
