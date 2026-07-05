import datetime
from unittest import TestCase

import jwt
from fastapi import Response

from app import schemas
from app.core.config import settings
from app.core.security import ALGORITHM, create_access_token, set_or_refresh_resource_token_cookie


class FakeURL:
    def __init__(self, scheme: str) -> None:
        self.scheme = scheme


class FakeRequest:
    """
    最小化的请求桩对象，仅提供 set_or_refresh_resource_token_cookie 所需属性。
    """

    def __init__(self, scheme: str, headers: dict | None = None, cookies: dict | None = None) -> None:
        self.url = FakeURL(scheme)
        self.headers = headers or {}
        self.cookies: dict = cookies or {}


class ResourceTokenCookieSecureFlagTest(TestCase):
    def _build_request_with_resource_cookie(
            self,
            *,
            userid: int = 1,
            username: str = "test",
            super_user: bool = False,
            level: int = 1,
            purpose: str = "resource"
    ) -> FakeRequest:
        resource_token = create_access_token(
            userid=userid,
            username=username,
            super_user=super_user,
            level=level,
            purpose=purpose,
        )
        return FakeRequest(
            scheme="https",
            cookies={settings.PROJECT_NAME: resource_token},
        )

    def _build_request_with_resource_secret_cookie(
            self,
            *,
            userid: int = 1,
            username: str = "test",
            super_user: bool = False,
            level: int = 1,
            purpose: str = "authentication"
    ) -> FakeRequest:
        now = datetime.datetime.now(datetime.UTC)
        resource_token = jwt.encode(
            {
                "exp": now + datetime.timedelta(seconds=settings.RESOURCE_ACCESS_TOKEN_EXPIRE_SECONDS),
                "iat": now,
                "sub": str(userid),
                "username": username,
                "super_user": super_user,
                "level": level,
                "purpose": purpose,
            },
            settings.RESOURCE_SECRET_KEY,
            algorithm=ALGORITHM,
        )
        return FakeRequest(
            scheme="https",
            cookies={settings.PROJECT_NAME: resource_token},
        )

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

    def test_existing_matching_resource_cookie_is_reused(self):
        request = self._build_request_with_resource_cookie()
        response = Response()
        payload = schemas.TokenPayload(sub=1, username="test", super_user=False, level=1)

        set_or_refresh_resource_token_cookie(request, response, payload)

        self.assertIsNone(response.headers.get("set-cookie"))

    def test_existing_resource_cookie_with_different_sub_is_replaced(self):
        request = self._build_request_with_resource_cookie(userid=2)
        response = Response()
        payload = schemas.TokenPayload(sub=1, username="test", super_user=False, level=1)

        set_or_refresh_resource_token_cookie(request, response, payload)

        self.assertIn(f"{settings.PROJECT_NAME}=", response.headers.get("set-cookie", ""))

    def test_existing_resource_cookie_with_different_username_is_replaced(self):
        request = self._build_request_with_resource_cookie(username="other")
        response = Response()
        payload = schemas.TokenPayload(sub=1, username="test", super_user=False, level=1)

        set_or_refresh_resource_token_cookie(request, response, payload)

        self.assertIn(f"{settings.PROJECT_NAME}=", response.headers.get("set-cookie", ""))

    def test_existing_resource_cookie_with_different_super_user_is_replaced(self):
        request = self._build_request_with_resource_cookie(super_user=True)
        response = Response()
        payload = schemas.TokenPayload(sub=1, username="test", super_user=False, level=1)

        set_or_refresh_resource_token_cookie(request, response, payload)

        self.assertIn(f"{settings.PROJECT_NAME}=", response.headers.get("set-cookie", ""))

    def test_existing_resource_cookie_with_different_level_is_replaced(self):
        request = self._build_request_with_resource_cookie(level=2)
        response = Response()
        payload = schemas.TokenPayload(sub=1, username="test", super_user=False, level=1)

        set_or_refresh_resource_token_cookie(request, response, payload)

        self.assertIn(f"{settings.PROJECT_NAME}=", response.headers.get("set-cookie", ""))

    def test_existing_resource_signed_cookie_with_wrong_purpose_is_replaced(self):
        request = self._build_request_with_resource_secret_cookie(purpose="authentication")
        response = Response()
        payload = schemas.TokenPayload(sub=1, username="test", super_user=False, level=1)

        set_or_refresh_resource_token_cookie(request, response, payload)

        self.assertIn(f"{settings.PROJECT_NAME}=", response.headers.get("set-cookie", ""))
