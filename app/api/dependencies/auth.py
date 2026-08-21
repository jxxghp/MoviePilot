"""用户身份、授权与认证服务依赖。"""

from typing import Any

from fastapi import Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.adapters.web.security.access import verify_token
from app.api.data import get_async_db, get_db
from app.api.dependencies.data import repository, standalone_repository
from app.application.security.auth import AuthService
from app.application.security.passkeys import PasskeyService
from app.application.security.user import UserService
from app.schemas.token import TokenPayload as _SchemaTokenPayload


def get_user_service(
    db: AsyncSession = Depends(get_async_db),
) -> UserService:
    """组装用户管理应用服务。"""
    return UserService(repository=repository("user", db))


def get_auth_service() -> AuthService:
    """组装同步认证应用服务。"""
    return AuthService(
        users=standalone_repository("user"),
        config=standalone_repository("system_config"),
        passkeys=standalone_repository("passkey"),
    )


def get_passkey_service() -> PasskeyService:
    """组装 PassKey 应用服务。"""
    return PasskeyService(repository=standalone_repository("passkey"))


def get_current_user(
    db: Session = Depends(get_db),
    token_data: _SchemaTokenPayload = Depends(verify_token),
) -> Any:
    """读取令牌对应用户，不存在时返回 403。"""
    user = repository("user", db).get_by_id(token_data.sub)
    if not user:
        raise HTTPException(status_code=403, detail="用户不存在")
    return user


async def get_current_user_async(
    db: AsyncSession = Depends(get_async_db),
    token_data: _SchemaTokenPayload = Depends(verify_token),
) -> Any:
    """异步读取令牌对应用户，不存在时返回 403。"""
    user = await repository("user", db).async_get_by_id(token_data.sub)
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
