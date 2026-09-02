"""认证、授权和当前用户自助更新契约测试。"""

import inspect
from http.cookies import SimpleCookie
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.testclient import TestClient
from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import Response

from app.adapters.web.security import access
from app.api.dependencies import auth as auth_dependencies
from app.api.endpoints import anthropic as anthropic_endpoint
from app.api.endpoints import auth as auth_endpoint
from app.api.endpoints import history as history_endpoint
from app.api.endpoints import mfa as mfa_endpoint
from app.api.endpoints import openai as openai_endpoint
from app.api.endpoints import subscribe as subscribe_endpoint
from app.api.endpoints import user as user_endpoint
from app.application.security import auth as auth_service_module
from app.application.security.auth import AuthService
from app.application.security.token import create_access_token, decode_access_token
from app.runtime.config import settings
from app.schemas.token import Token, TokenPayload
from app.schemas.user import CurrentUserUpdate


def _request() -> Request:
    """构造认证端点所需的最小请求。"""
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/auth/exchange",
            "headers": [(b"host", b"testserver")],
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("testclient", 123),
        }
    )


def _user(*, active: bool = True, superuser: bool = False) -> SimpleNamespace:
    """构造认证依赖所需的最小用户投影。"""
    return SimpleNamespace(
        id=7,
        name="tester",
        is_active=active,
        is_superuser=superuser,
        avatar="avatar",
        permissions={},
    )


def test_current_user_update_rejects_admin_fields():
    """自助资料模型必须拒绝用户名、状态和权限等管理字段。"""
    assert set(CurrentUserUpdate.model_fields) == {
        "email",
        "avatar",
        "settings",
        "password",
    }
    with pytest.raises(ValidationError):
        CurrentUserUpdate(name="attacker")


@pytest.mark.asyncio
async def test_update_current_user_uses_id_and_returns_updated_snapshot():
    """自助更新必须按当前用户 ID 写入白名单字段并返回新快照。"""
    updated_user = _user()
    service = SimpleNamespace(update=AsyncMock(return_value=updated_user))
    user_input = CurrentUserUpdate(
        email="tester@example.com",
        settings={"nickname": "Tester", "telegram_userid": "42"},
        password="Abc123!",
    )

    with patch.object(user_endpoint, "get_password_hash", return_value="hashed"):
        result = await user_endpoint.update_current_user(
            service=service,
            user_in=user_input,
            current_user=SimpleNamespace(id=7),
        )

    assert result is updated_user
    service.update.assert_awaited_once_with(
        7,
        {
            "email": "tester@example.com",
            "settings": {"nickname": "Tester", "telegram_userid": "42"},
            "hashed_password": "hashed",
        },
    )


@pytest.mark.asyncio
async def test_update_current_user_does_not_overwrite_password_with_empty_value():
    """空密码只表示不修改密码，不能作为持久化字段传入服务。"""
    updated_user = _user()
    service = SimpleNamespace(update=AsyncMock(return_value=updated_user))

    result = await user_endpoint.update_current_user(
        service=service,
        user_in=CurrentUserUpdate(password=""),
        current_user=SimpleNamespace(id=7),
    )

    assert result is updated_user
    service.update.assert_awaited_once_with(7, {})


@pytest.mark.asyncio
async def test_update_current_user_returns_http_error_for_invalid_password():
    """自助更新密码失败时必须返回与 User 响应模型一致的 HTTP 错误。"""
    service = SimpleNamespace(update=AsyncMock())

    with pytest.raises(HTTPException) as exc_info:
        await user_endpoint.update_current_user(
            service=service,
            user_in=CurrentUserUpdate(password="password"),
            current_user=SimpleNamespace(id=7),
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "密码须为6至50位，并包含字母、数字、特殊字符中的至少两类"
    service.update.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_current_user_treats_missing_target_as_expired_authentication():
    """当前用户在写入前消失时必须提示令牌失效，而不是伪报成功。"""
    service = SimpleNamespace(update=AsyncMock(return_value=None))

    with pytest.raises(HTTPException) as exc_info:
        await user_endpoint.update_current_user(
            service=service,
            user_in=CurrentUserUpdate(email="tester@example.com"),
            current_user=SimpleNamespace(id=7),
        )

    assert exc_info.value.status_code == 401
    assert exc_info.value.headers == {"WWW-Authenticate": "Bearer"}


def test_current_user_dependency_uses_active_user_before_superuser_check():
    """超级管理员依赖必须先经过激活状态校验。"""
    assert (
        inspect.signature(auth_dependencies.get_current_active_superuser)
        .parameters["current_user"]
        .default.dependency
        is auth_dependencies.get_current_active_user
    )
    assert (
        inspect.signature(auth_dependencies.get_current_active_superuser_async)
        .parameters["current_user"]
        .default.dependency
        is auth_dependencies.get_current_active_user_async
    )


def test_current_user_update_requires_active_user_dependency():
    """自助资料写入必须使用激活用户依赖，不能接受裸令牌。"""
    assert (
        inspect.signature(user_endpoint.update_current_user)
        .parameters["current_user"]
        .default.dependency
        is auth_dependencies.get_current_active_user_async
    )


@pytest.mark.parametrize(
    "dependency",
    [
        auth_dependencies.get_current_active_user,
    ],
)
def test_inactive_user_is_unauthorized_for_sync_dependencies(dependency):
    """停用用户的既有令牌在同步依赖路径上必须立即失效。"""
    with pytest.raises(HTTPException) as exc_info:
        dependency(current_user=_user(active=False, superuser=True))

    assert exc_info.value.status_code == 401
    assert exc_info.value.headers == {"WWW-Authenticate": "Bearer"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "dependency",
    [
        auth_dependencies.get_current_active_user_async,
    ],
)
async def test_inactive_user_is_unauthorized_for_async_dependencies(dependency):
    """停用用户的既有令牌在异步依赖路径上必须立即失效。"""
    with pytest.raises(HTTPException) as exc_info:
        await dependency(current_user=_user(active=False, superuser=True))

    assert exc_info.value.status_code == 401
    assert exc_info.value.headers == {"WWW-Authenticate": "Bearer"}


def test_regular_user_is_forbidden_for_superuser_dependency():
    """已认证但无超级管理员权限的用户必须得到 403。"""
    with pytest.raises(HTTPException) as exc_info:
        auth_dependencies.get_current_active_superuser(
            current_user=_user(active=True, superuser=False)
        )

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == "用户权限不足"


@pytest.mark.asyncio
async def test_reading_other_user_is_forbidden_for_regular_user():
    """普通用户不能通过用户名读取其他账号资料。"""
    service = SimpleNamespace(
        get_by_name=AsyncMock(return_value=_user()),
    )

    with pytest.raises(HTTPException) as exc_info:
        await user_endpoint.read_user_by_name(
            username="other",
            current_user=SimpleNamespace(
                id=8,
                name="member",
                is_superuser=False,
            ),
            service=service,
        )

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == "用户权限不足"


def test_missing_current_user_is_unauthorized():
    """令牌对应用户不存在时应按认证失败返回 401。"""
    repository = SimpleNamespace(get_by_id=Mock(return_value=None))
    runtime = SimpleNamespace(
        authentication=SimpleNamespace(user_repository=lambda _db: repository)
    )

    with pytest.raises(HTTPException) as exc_info:
        auth_dependencies.get_current_user(
            db=object(),
            token_data=SimpleNamespace(sub=7),
            runtime=runtime,
        )

    assert exc_info.value.status_code == 401
    assert exc_info.value.headers == {"WWW-Authenticate": "Bearer"}


def test_invalid_jwt_is_unauthorized_with_bearer_challenge():
    """JWT 解码失败不得被误报为授权不足。"""
    with patch.object(access, "_decode_token", side_effect=ValueError("invalid token")):
        with pytest.raises(HTTPException) as exc_info:
            access._decode_or_http_error("invalid", "authentication")

    assert exc_info.value.status_code == 401
    assert exc_info.value.headers == {"WWW-Authenticate": "Bearer"}


@pytest.mark.parametrize("state", ["inactive", "renamed", "demoted"])
def test_verify_token_revalidates_current_user_state(monkeypatch, state):
    """裸 JWT 依赖必须在每次请求重新确认账号状态与身份声明。"""
    current_user = _user(superuser=True)
    if state == "inactive":
        current_user.is_active = False
    elif state == "renamed":
        current_user.name = "renamed"
    else:
        current_user.is_superuser = False

    def validate(payload):
        """模拟组合根提供的当前用户身份校验端口。"""
        if not current_user.is_active:
            raise PermissionError("用户不存在或已禁用")
        if (
            payload.username != current_user.name
            or payload.super_user != current_user.is_superuser
        ):
            raise PermissionError("令牌身份或权限上下文不匹配")

    monkeypatch.setattr(access, "_token_identity_validator", validate)
    token = create_access_token(
        userid=current_user.id,
        username="tester",
        super_user=True,
    )

    with pytest.raises(HTTPException) as exc_info:
        access.verify_token(_request(), Response(), token, None, None)

    assert exc_info.value.status_code == 401
    assert exc_info.value.headers == {"WWW-Authenticate": "Bearer"}


def test_verify_resource_token_revalidates_current_user_state(monkeypatch):
    """资源 Cookie 不能绕过账号停用检查。"""
    current_user = _user(active=False)

    def validate(_payload):
        """模拟组合根提供的当前用户身份校验端口。"""
        if not current_user.is_active:
            raise PermissionError("用户不存在或已禁用")

    monkeypatch.setattr(access, "_token_identity_validator", validate)
    token = create_access_token(
        userid=current_user.id,
        username=current_user.name,
        super_user=current_user.is_superuser,
        purpose="resource",
    )

    with pytest.raises(HTTPException) as exc_info:
        access.verify_resource_token(token)

    assert exc_info.value.status_code == 401
    assert exc_info.value.headers == {"WWW-Authenticate": "Bearer"}


@pytest.mark.parametrize("credential", ["api_key", "api_token"])
def test_verify_token_revalidates_api_credential_identity(monkeypatch, credential):
    """API Key 与 API Token 映射的超级管理员停用后必须立即失效。"""
    monkeypatch.setattr(access, "get_runtime_setting", lambda key: "api-secret")
    monkeypatch.setattr(
        access,
        "_superuser_token_payload_provider",
        Mock(side_effect=PermissionError("用户不存在或已禁用")),
    )
    api_key = "api-secret" if credential == "api_key" else None
    api_token = "api-secret" if credential == "api_token" else None

    with pytest.raises(HTTPException) as exc_info:
        access.verify_token(_request(), Response(), None, api_key, api_token)

    assert exc_info.value.status_code == 401
    assert exc_info.value.headers == {"WWW-Authenticate": "Bearer"}


@pytest.mark.parametrize("dependency", [access.verify_apikey, access.verify_apitoken])
def test_standalone_api_credential_revalidates_current_identity(monkeypatch, dependency):
    """独立 API 凭据依赖也必须执行当前用户状态校验。"""
    monkeypatch.setattr(access, "get_runtime_setting", lambda key: "api-secret")
    monkeypatch.setattr(
        access,
        "_superuser_token_payload_provider",
        Mock(side_effect=PermissionError("用户不存在或已禁用")),
    )

    with pytest.raises(HTTPException) as exc_info:
        dependency("api-secret")

    assert exc_info.value.status_code == 401
    assert exc_info.value.headers == {"WWW-Authenticate": "Bearer"}


def test_api_token_reader_prefers_header_and_preserves_query_compatibility():
    """兼容 Token 依赖应优先使用请求头，同时继续接受旧查询参数。"""
    assert access._get_api_token("query-token", "header-token") == "header-token"
    assert access._get_api_token("query-token", None) == "query-token"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("endpoint", "credential"),
    [
        (
            openai_endpoint,
            HTTPAuthorizationCredentials(scheme="Bearer", credentials="api-secret"),
        ),
        (anthropic_endpoint, "api-secret"),
    ],
)
async def test_agent_protocol_api_credentials_revalidate_current_identity(
    monkeypatch,
    endpoint,
    credential,
):
    """Agent 兼容协议必须保留各自错误结构并拒绝停用账号的 API 凭据。"""
    monkeypatch.setattr(
        endpoint,
        "get_api_runtime_config_snapshot",
        lambda: SimpleNamespace(api_token="api-secret"),
    )
    monkeypatch.setattr(
        endpoint,
        "validate_api_credential_identity",
        Mock(side_effect=HTTPException(status_code=401, detail="用户不存在或已禁用")),
    )

    response = await endpoint._check_auth(credential)

    assert response is not None
    assert response.status_code == 401
    assert b'"type":"authentication_error"' in response.body


@pytest.mark.asyncio
async def test_seerr_api_credential_revalidates_before_reading_payload(monkeypatch):
    """Seerr 管理员级 Webhook 必须在处理报文前拒绝停用账号身份。"""
    request = SimpleNamespace(json=AsyncMock())
    monkeypatch.setattr(
        subscribe_endpoint,
        "get_api_runtime_config_snapshot",
        lambda: SimpleNamespace(api_token="api-secret"),
    )
    monkeypatch.setattr(
        subscribe_endpoint,
        "validate_api_credential_identity",
        Mock(side_effect=HTTPException(status_code=401, detail="用户不存在或已禁用")),
    )

    with pytest.raises(HTTPException) as exc_info:
        await subscribe_endpoint.seerr_subscribe(
            request=request,
            task_registry=SimpleNamespace(),
            authorization="api-secret",
        )

    assert exc_info.value.status_code == 401
    request.json.assert_not_awaited()


def test_api_credential_provider_does_not_cache_identity(monkeypatch):
    """API 凭据每次都应重新解析超级管理员绑定，不能锁定旧身份。"""
    first = auth_endpoint._SchemaTokenPayload(
        sub=7,
        username="first",
        super_user=True,
        level=1,
        purpose="authentication",
    )
    second = auth_endpoint._SchemaTokenPayload(
        sub=8,
        username="second",
        super_user=True,
        level=1,
        purpose="authentication",
    )
    payloads = iter([first, second])
    calls = []

    def provider():
        """返回当前配置绑定的超级管理员载荷。"""
        calls.append(True)
        return next(payloads)

    monkeypatch.setattr(access, "get_runtime_setting", lambda key: "api-secret")
    monkeypatch.setattr(access, "_superuser_token_payload_provider", provider)
    monkeypatch.setattr(access, "_token_identity_validator", lambda _payload: None)

    first_result = access.verify_token(_request(), Response(), None, "api-secret", None)
    second_result = access.verify_token(_request(), Response(), None, "api-secret", None)

    assert first_result is first
    assert second_result is second
    assert len(calls) == 2


def test_superuser_payload_provider_rejects_inactive_user(monkeypatch):
    """超级管理员 API 凭据载荷提供器不能为停用账号签发身份。"""
    user = _user(active=False, superuser=True)
    service = AuthService(
        users=SimpleNamespace(get_by_name=Mock(return_value=user)),
        config=SimpleNamespace(),
        passkeys=SimpleNamespace(),
    )
    monkeypatch.setattr(
        auth_service_module,
        "get_chain_runtime_config_snapshot",
        lambda: SimpleNamespace(superuser=user.name),
    )

    with pytest.raises(PermissionError):
        service.build_superuser_token_payload()


def test_superuser_payload_provider_falls_back_to_database_admin(monkeypatch):
    """V2 升级后 SUPERUSER 为空时，API 凭据应绑定现有启用管理员。"""
    user = _user(active=True, superuser=True)
    users = SimpleNamespace(
        get_by_name=Mock(),
        get_active_superuser=Mock(return_value=user),
    )
    service = AuthService(
        users=users,
        config=SimpleNamespace(),
        passkeys=SimpleNamespace(),
    )
    monkeypatch.setattr(
        auth_service_module,
        "get_chain_runtime_config_snapshot",
        lambda: SimpleNamespace(superuser=""),
    )

    payload = service.build_superuser_token_payload()

    assert payload.sub == user.id
    assert payload.username == user.name
    users.get_by_name.assert_not_called()
    users.get_active_superuser.assert_called_once_with()


def test_superuser_payload_provider_explains_missing_binding(monkeypatch):
    """配置和数据库都没有管理员时，应返回可操作的认证失败原因。"""
    service = AuthService(
        users=SimpleNamespace(get_active_superuser=Mock(return_value=None)),
        config=SimpleNamespace(),
        passkeys=SimpleNamespace(),
    )
    monkeypatch.setattr(
        auth_service_module,
        "get_chain_runtime_config_snapshot",
        lambda: SimpleNamespace(superuser=""),
    )

    with pytest.raises(
        PermissionError,
        match="未配置 SUPERUSER，且数据库中没有可用超级管理员",
    ):
        service.build_superuser_token_payload()


def test_auth_service_reads_user_by_id_and_accepts_current_token_identity():
    """认证服务按稳定用户 ID 查询，并接受与当前账号一致的令牌声明。"""
    user = _user()
    users = SimpleNamespace(get_by_id=Mock(return_value=user))
    service = AuthService(
        users=users,
        config=SimpleNamespace(),
        passkeys=SimpleNamespace(),
    )

    assert service.get_user_by_id(user.id) is user
    service.validate_token_identity(
        TokenPayload(
            sub=user.id,
            username=user.name,
            super_user=user.is_superuser,
            level=1,
            purpose="authentication",
        )
    )

    assert users.get_by_id.call_count == 2


def test_superuser_payload_provider_follows_stable_id_after_admin_rename(monkeypatch):
    """管理员改名后，API 凭据应继续绑定原用户而非旧配置用户名。"""
    original = _user(superuser=True)
    renamed = _user(superuser=True)
    renamed.name = "renamed"
    users = SimpleNamespace(
        get_by_name=Mock(return_value=original),
        get_by_id=Mock(return_value=renamed),
    )
    service = AuthService(
        users=users,
        config=SimpleNamespace(),
        passkeys=SimpleNamespace(),
    )
    monkeypatch.setattr(
        auth_service_module,
        "get_chain_runtime_config_snapshot",
        lambda: SimpleNamespace(superuser="tester"),
    )

    first = service.build_superuser_token_payload()
    second = service.build_superuser_token_payload()

    assert first.username == "tester"
    assert second.username == "renamed"
    users.get_by_name.assert_called_once_with("tester")
    users.get_by_id.assert_called_once_with(original.id)


def test_bare_verify_token_rejects_inactive_user_on_real_history_route(monkeypatch):
    """真实历史直达路由不能仅凭停用用户的旧 JWT 通过。"""
    current_user = _user(active=False)

    def validate(_payload):
        """模拟组合根提供的当前用户身份校验端口。"""
        if not current_user.is_active:
            raise PermissionError("用户不存在或已禁用")

    monkeypatch.setattr(access, "_token_identity_validator", validate)
    query = SimpleNamespace(list_download=AsyncMock(return_value=[]))
    app = FastAPI()
    app.include_router(history_endpoint.router, prefix="/history")
    app.dependency_overrides[history_endpoint.get_history_query_service] = lambda: query
    token = create_access_token(
        userid=current_user.id,
        username=current_user.name,
        super_user=current_user.is_superuser,
    )

    with TestClient(app) as client:
        response = client.get(
            "/history/download",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    query.list_download.assert_not_awaited()


def test_anonymous_mfa_status_contract_is_removed():
    """匿名 MFA 用户名状态查询不再属于公开路由或 schema 契约。"""
    assert not hasattr(mfa_endpoint, "mfa_status")
    assert not any(route.path == "/status/{username}" for route in mfa_endpoint.router.routes)


def test_auth_exchange_sets_resource_cookie():
    """插件票据兑换必须和密码登录一样建立资源令牌 Cookie。"""
    user = _user()
    token = Token(
        access_token="access-token",
        token_type="bearer",
        super_user=False,
        user_id=user.id,
        user_name=user.name,
        avatar=user.avatar,
        level=3,
        permissions={},
    )
    service = SimpleNamespace(
        get_user_by_id=Mock(return_value=user),
        build_token_response=Mock(return_value=token),
    )
    response = Response()

    with patch.object(
        auth_endpoint,
        "consume_plugin_auth_ticket",
        return_value={"user_id": user.id},
    ):
        result = auth_endpoint.auth_exchange(
            request=_request(),
            response=response,
            body=auth_endpoint.AuthExchangeRequest(ticket="ticket"),
            service=service,
        )

    assert result is token
    cookies = SimpleCookie(response.headers["set-cookie"])
    resource_cookie = cookies[settings.PROJECT_NAME]
    assert resource_cookie["httponly"]
    assert resource_cookie["samesite"].lower() == "lax"
    payload = decode_access_token(resource_cookie.value, "resource")
    assert payload.model_dump() == {
        "sub": user.id,
        "username": user.name,
        "super_user": user.is_superuser,
        "level": token.level,
        "purpose": "resource",
    }


@pytest.mark.parametrize("user", [None, _user(active=False)])
def test_auth_exchange_rejects_missing_or_disabled_user(user):
    """票据对应账号不存在或停用时不得签发 Token。"""
    service = SimpleNamespace(
        get_user_by_id=Mock(return_value=user),
        build_token_response=Mock(),
    )

    response = Response()
    with patch.object(
        auth_endpoint,
        "consume_plugin_auth_ticket",
        return_value={"user_id": 7},
    ), pytest.raises(HTTPException) as exc_info:
        auth_endpoint.auth_exchange(
            request=_request(),
            response=response,
            body=auth_endpoint.AuthExchangeRequest(ticket="ticket"),
            service=service,
        )

    assert exc_info.value.status_code == 401
    assert exc_info.value.headers == {"WWW-Authenticate": "Bearer"}
    assert "set-cookie" not in response.headers
    service.build_token_response.assert_not_called()
