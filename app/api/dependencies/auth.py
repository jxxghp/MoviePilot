"""用户身份、授权与认证服务依赖。"""

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
from app.application.security.passkey import PasskeyRepository, PasskeyService
from app.application.security.user import (
    AsyncUnitOfWork,
    UserRepository,
    UserService,
)
from app.application.security.userconfig import get_configured_user_configuration
from app.schemas.token import TokenPayload as _SchemaTokenPayload
from app.startup.composition.context import HostRuntime


def get_user_service(
    db: AsyncSession = Depends(get_async_session),
    runtime: HostRuntime = Depends(get_host_runtime),
) -> UserService:
    """组装用户管理应用服务。"""
    return UserService(
        repository=cast(
            UserRepository, runtime.authentication.user_repository(db)
        ),
        unit_of_work=cast(
            AsyncUnitOfWork, runtime.persistence.async_transaction(db)
        ),
        configuration=get_configured_user_configuration(),
    )


def get_auth_service(
    runtime: HostRuntime = Depends(get_host_runtime),
) -> AuthService:
    """组装同步认证应用服务。"""
    return AuthService(
        users=cast(AuthUserRepository, runtime.authentication.standalone_user()),
        config=cast(AuthConfigRepository, runtime.authentication.system_config()),
        passkeys=cast(AuthPasskeyRepository, runtime.authentication.passkey()),
    )


def get_passkey_service(
    runtime: HostRuntime = Depends(get_host_runtime),
) -> PasskeyService:
    """组装 PassKey 应用服务。"""
    return PasskeyService(repository=cast(
        PasskeyRepository, runtime.authentication.passkey()
    ))


def get_current_user(
    db: Session = Depends(get_sync_session),
    token_data: _SchemaTokenPayload = Depends(verify_token),
    runtime: HostRuntime = Depends(get_host_runtime),
) -> Any:
    """读取令牌对应用户，不存在时返回 403。"""
    user_repository = cast(
        AuthUserRepository, runtime.authentication.user_repository(db)
    )
    user = user_repository.get_by_id(token_data.sub)
    if not user:
        raise HTTPException(status_code=403, detail="用户不存在")
    return user


async def get_current_user_async(
    db: AsyncSession = Depends(get_async_session),
    token_data: _SchemaTokenPayload = Depends(verify_token),
    runtime: HostRuntime = Depends(get_host_runtime),
) -> Any:
    """异步读取令牌对应用户，不存在时返回 403。"""
    user_repository = cast(
        UserRepository, runtime.authentication.user_repository(db)
    )
    user = await user_repository.async_get_by_id(token_data.sub)
    if not user:
        raise HTTPException(status_code=403, detail="用户不存在")
    return user


def get_current_active_user(
    current_user: Any = Depends(get_current_user),
) -> Any:
    """校验并返回当前激活用户。"""
    if not current_user.is_active:
        raise HTTPException(status_code=403, detail="用户未激活")
    return current_user


async def get_current_active_user_async(
    current_user: Any = Depends(get_current_user_async),
) -> Any:
    """异步校验并返回当前激活用户。"""
    if not current_user.is_active:
        raise HTTPException(status_code=403, detail="用户未激活")
    return current_user


def _ensure_manage_user(current_user: Any) -> Any:
    """校验用户具备全局管理权限。"""
    permissions = current_user.permissions or {}
    if not current_user.is_superuser and not bool(permissions.get("manage")):
        raise HTTPException(status_code=400, detail="用户权限不足")
    return current_user


def get_current_active_manage_user(
    current_user: Any = Depends(get_current_active_user),
) -> Any:
    """返回当前拥有管理权限的激活用户。"""
    return _ensure_manage_user(current_user)


async def get_current_active_manage_user_async(
    current_user: Any = Depends(get_current_active_user_async),
) -> Any:
    """异步返回当前拥有管理权限的激活用户。"""
    return _ensure_manage_user(current_user)


def get_current_active_superuser(
    current_user: Any = Depends(get_current_user),
) -> Any:
    """校验并返回当前激活超级管理员。"""
    if not current_user.is_superuser:
        raise HTTPException(status_code=400, detail="用户权限不足")
    return current_user


async def get_current_active_superuser_async(
    current_user: Any = Depends(get_current_user_async),
) -> Any:
    """异步校验并返回当前激活超级管理员。"""
    if not current_user.is_superuser:
        raise HTTPException(status_code=400, detail="用户权限不足")
    return current_user
