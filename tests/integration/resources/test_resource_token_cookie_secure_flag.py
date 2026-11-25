"""
验证资源 Cookie 的协议安全属性、身份匹配及用途校验。
"""

import datetime

import jwt
from fastapi import Response

from app import schemas
from app.adapters.web.security.access import set_or_refresh_resource_token_cookie
from app.application.security.token import ALGORITHM, create_access_token
from app.runtime.config import settings


class FakeURL:
    """
    仅保留协议字段，供资源 Cookie 安全属性判断使用。
    """

    def __init__(self, scheme: str) -> None:
        """
        保存测试 URL 的协议名称。
        """
        self.scheme = scheme


class FakeRequest:
    """
    最小化的请求桩对象，仅提供 set_or_refresh_resource_token_cookie 所需属性。
    """

    def __init__(self, scheme: str, headers: dict | None = None, cookies: dict | None = None) -> None:
        """
        保存测试请求使用的协议、请求头或 Cookie 数据。
        """
        self.url = FakeURL(scheme)
        self.headers = headers or {}
        self.cookies: dict = cookies or {}


def _build_request_with_resource_cookie(
    *, userid: int = 1, username: str = "test", super_user: bool = False, level: int = 1, purpose: str = "resource"
) -> FakeRequest:
    """
    构造使用访问令牌签名的资源 Cookie 请求。

    :param userid: 令牌所属用户标识，默认 1
    :param username: 令牌内用户名，默认 test
    :param super_user: 令牌声明的管理员标志，默认 False
    :param level: 令牌声明的用户等级，默认 1
    :param purpose: 令牌用途，默认 resource
    :return: 携带已签发 Cookie 的 HTTPS 请求桩
    """
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
    *,
    userid: int = 1,
    username: str = "test",
    super_user: bool = False,
    level: int = 1,
    purpose: str = "authentication",
) -> FakeRequest:
    """
    构造由资源专用密钥签名的 Cookie 请求。

    :param userid: 令牌所属用户标识，默认 1
    :param username: 令牌内用户名，默认 test
    :param super_user: 令牌声明的管理员标志，默认 False
    :param level: 令牌声明的用户等级，默认 1
    :param purpose: 令牌用途，默认 authentication，用于验证用途不符时的替换
    :return: 携带资源密钥签名 Cookie 的 HTTPS 请求桩
    """
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


def test_secure_flag_set_when_https_terminated_at_reverse_proxy() -> None:
    """
    当反向代理（如 nginx）终止 HTTPS 并以 HTTP 转发给后端时，
    资源令牌 Cookie 仍必须携带 secure 属性，不能因为直连请求协议是 http 就降级。
    """
    request = FakeRequest(scheme="http", headers={"x-forwarded-proto": "https"})
    response = Response()
    payload = schemas.TokenPayload(sub=1, username="test", super_user=False, level=1)

    set_or_refresh_resource_token_cookie(request, response, payload)

    set_cookie_header = response.headers.get("set-cookie", "")
    assert "Secure" in set_cookie_header


def test_existing_matching_resource_cookie_is_reused() -> None:
    """
    已有资源 Cookie 身份与用途匹配时直接复用。
    """
    request = _build_request_with_resource_cookie()
    response = Response()
    payload = schemas.TokenPayload(sub=1, username="test", super_user=False, level=1)

    set_or_refresh_resource_token_cookie(request, response, payload)

    assert response.headers.get("set-cookie") is None


def test_existing_resource_cookie_with_different_sub_is_replaced() -> None:
    """
    已有资源 Cookie 用户标识不符时重新签发。
    """
    request = _build_request_with_resource_cookie(userid=2)
    response = Response()
    payload = schemas.TokenPayload(sub=1, username="test", super_user=False, level=1)

    set_or_refresh_resource_token_cookie(request, response, payload)

    assert f"{settings.PROJECT_NAME}=" in response.headers.get("set-cookie", "")


def test_existing_resource_cookie_with_different_username_is_replaced() -> None:
    """
    已有资源 Cookie 用户名不符时重新签发。
    """
    request = _build_request_with_resource_cookie(username="other")
    response = Response()
    payload = schemas.TokenPayload(sub=1, username="test", super_user=False, level=1)

    set_or_refresh_resource_token_cookie(request, response, payload)

    assert f"{settings.PROJECT_NAME}=" in response.headers.get("set-cookie", "")


def test_existing_resource_cookie_with_different_super_user_is_replaced() -> None:
    """
    已有资源 Cookie 管理员标志不符时重新签发。
    """
    request = _build_request_with_resource_cookie(super_user=True)
    response = Response()
    payload = schemas.TokenPayload(sub=1, username="test", super_user=False, level=1)

    set_or_refresh_resource_token_cookie(request, response, payload)

    assert f"{settings.PROJECT_NAME}=" in response.headers.get("set-cookie", "")


def test_existing_resource_cookie_with_different_level_is_replaced() -> None:
    """
    已有资源 Cookie 用户等级不符时重新签发。
    """
    request = _build_request_with_resource_cookie(level=2)
    response = Response()
    payload = schemas.TokenPayload(sub=1, username="test", super_user=False, level=1)

    set_or_refresh_resource_token_cookie(request, response, payload)

    assert f"{settings.PROJECT_NAME}=" in response.headers.get("set-cookie", "")


def test_existing_resource_signed_cookie_with_wrong_purpose_is_replaced() -> None:
    """
    资源密钥签名的 Cookie 用途不符时也必须重新签发。
    """
    request = _build_request_with_resource_secret_cookie(purpose="authentication")
    response = Response()
    payload = schemas.TokenPayload(sub=1, username="test", super_user=False, level=1)

    set_or_refresh_resource_token_cookie(request, response, payload)

    assert f"{settings.PROJECT_NAME}=" in response.headers.get("set-cookie", "")
