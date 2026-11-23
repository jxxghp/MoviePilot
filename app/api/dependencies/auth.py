"""
用户身份、授权与认证服务依赖。
"""

from typing import Any, cast

from fastapi import Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.adapters.web.security.access import verify_token
from app.api.context import get_async_session, get_host_runtime, get_sync_session
from app.application.security.auth import (
    AuthConfigRepository,
    AuthPasskeyRepository,
    AuthService,
    AuthUserRepository,
)
from app.application.security.authorization import get_channel_authorization
from app.application.security.passkey import PasskeyRepository, PasskeyService
from app.application.security.user import (
    UserRepository,
    UserService,
)
from app.application.security.userconfig import get_configured_user_configuration
from app.domain.authorization import has_web_permission
from app.schemas.token import TokenPayload as _SchemaTokenPayload
from app.startup.composition.context import HostRuntime


def get_user_service(
    db: AsyncSession = Depends(get_async_session),
    runtime: HostRuntime = Depends(get_host_runtime),
) -> UserService:
    """
    组装用户管理应用服务。
    """
    return UserService(
        repository=cast(
            UserRepository, runtime.authentication.user_repository(db)
        ),
        unit_of_work=runtime.persistence.async_transaction(db),
        configuration=get_configured_user_configuration(),
    )


def get_auth_service(
    runtime: HostRuntime = Depends(get_host_runtime),
) -> AuthService:
    """
    组装同步认证应用服务。
    """
    return AuthService(
        users=cast(AuthUserRepository, runtime.authentication.standalone_user()),
        config=cast(AuthConfigRepository, runtime.authentication.system_config()),
        passkeys=cast(AuthPasskeyRepository, runtime.authentication.passkey()),
    )


def get_passkey_service(
    runtime: HostRuntime = Depends(get_host_runtime),
) -> PasskeyService:
    """
    组装 PassKey 应用服务。
    """
    return PasskeyService(repository=cast(
        PasskeyRepository, runtime.authentication.passkey()
    ))


def get_current_user(
    db: Session = Depends(get_sync_session),
    token_data: _SchemaTokenPayload = Depends(verify_token),
    runtime: HostRuntime = Depends(get_host_runtime),
) -> Any:
    """
    读取令牌对应用户；主体 ID 缺失或账号不存在时按认证失败处理。
    """
    if token_data.agent_authorization_id:
        return _channel_principal(token_data)
    user_repository = cast(
        AuthUserRepository, runtime.authentication.user_repository(db)
    )
    user = user_repository.get_by_id(token_data.sub) if token_data.sub is not None else None
    if not user:
        raise HTTPException(
            status_code=401,
            detail="用户不存在",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


async def get_current_user_async(
    db: AsyncSession = Depends(get_async_session),
    token_data: _SchemaTokenPayload = Depends(verify_token),
    runtime: HostRuntime = Depends(get_host_runtime),
) -> Any:
    """
    异步读取令牌对应用户；主体 ID 缺失或账号不存在时按认证失败处理。
    """
    if token_data.agent_authorization_id:
        from app.runtime.execution import run_in_threadpool

        return await run_in_threadpool(_channel_principal, token_data)
    user_repository = cast(
        UserRepository, runtime.authentication.user_repository(db)
    )
    user = await user_repository.async_get_by_id(token_data.sub) if token_data.sub is not None else None
    if not user:
        raise HTTPException(
            status_code=401,
            detail="用户不存在",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


def _channel_principal(token_data: _SchemaTokenPayload) -> Any:
    """
    为当前请求读取实时渠道权限，不把底层内部账号当作普通网页登录账号。
    """
    service = get_channel_authorization()
    if service is None or not token_data.agent_authorization_id or not token_data.agent_operation:
        raise HTTPException(status_code=401, detail="渠道授权不可用")
    try:
        return service.authorize_operation(token_data.agent_authorization_id, token_data.agent_operation)
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error


def get_current_active_user(
    current_user: Any = Depends(get_current_user),
) -> Any:
    """
    校验并返回当前激活用户。
    """
    if not current_user.is_active:
        raise HTTPException(
            status_code=401,
            detail="用户未激活",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return current_user


async def get_current_active_user_async(
    current_user: Any = Depends(get_current_user_async),
) -> Any:
    """
    异步校验并返回当前激活用户。
    """
    if not current_user.is_active:
        raise HTTPException(
            status_code=401,
            detail="用户未激活",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return current_user


def _ensure_manage_user(current_user: Any) -> Any:
    """
    校验用户具备全局管理权限。
    """
    permissions = current_user.permissions or {}
    if not current_user.is_superuser and not bool(permissions.get("manage")):
        raise HTTPException(status_code=403, detail="用户权限不足")
    return current_user


def get_current_download_viewer(current_user: Any = Depends(get_current_active_user)) -> Any:
    """
    允许管理员、下载管理用户查看全局下载；渠道成员及具备搜索/订阅权限的普通网页用户仅可查看本人下载。

    :param current_user: 当前已认证激活用户
    :return: 通过校验的当前用户
    """
    if getattr(current_user, "authorization_id", None) and current_user.permissions.get("download") is True:
        return current_user
    permissions = getattr(current_user, "permissions", None)
    if current_user.is_superuser or has_web_permission(False, permissions, "manage", "manage.downloading"):
        return current_user
    if has_web_permission(False, permissions, "search", "search.resource") or has_web_permission(
        False, permissions, "subscribe"
    ):
        return current_user
    raise HTTPException(status_code=403, detail="用户权限不足")


def get_current_active_manage_user(
    current_user: Any = Depends(get_current_active_user),
) -> Any:
    """
    返回当前拥有管理权限的激活用户。
    """
    return _ensure_manage_user(current_user)


async def get_current_active_manage_user_async(
    current_user: Any = Depends(get_current_active_user_async),
) -> Any:
    """
    异步返回当前拥有管理权限的激活用户。
    """
    return _ensure_manage_user(current_user)


def get_current_active_superuser(
    current_user: Any = Depends(get_current_active_user),
) -> Any:
    """
    校验并返回当前激活超级管理员。
    """
    if not current_user.is_superuser:
        raise HTTPException(status_code=403, detail="用户权限不足")
    return current_user


async def get_current_active_superuser_async(
    current_user: Any = Depends(get_current_active_user_async),
) -> Any:
    """
    异步校验并返回当前激活超级管理员。
    """
    if not current_user.is_superuser:
        raise HTTPException(status_code=403, detail="用户权限不足")
    return current_user
