"""按令牌解析当前用户的路由依赖。

每个依赖沿 ``verify_token`` -> 用户实体 -> 激活状态 -> 权限等级逐级收紧，校验不通过
一律抛出 HTTPException。可被 HTTP 端点与面向插件的导入面共用。
"""
from fastapi import Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.adapters.web.security.access import verify_token
from app.db import get_async_db, get_db
from app.db.models.user import User
from app.schemas.token import TokenPayload as _SchemaTokenPayload


def get_current_user(
        db: Session = Depends(get_db),
        token_data: _SchemaTokenPayload = Depends(verify_token)
) -> User:
    """
    获取当前用户
    """
    user = User.get(db, rid=token_data.sub)
    if not user:
        raise HTTPException(status_code=403, detail="用户不存在")
    return user


async def get_current_user_async(
        db: AsyncSession = Depends(get_async_db),
        token_data: _SchemaTokenPayload = Depends(verify_token)
) -> User:
    """
    异步获取当前用户
    """
    user = await User.async_get(db, rid=token_data.sub)
    if not user:
        raise HTTPException(status_code=403, detail="用户不存在")
    return user


def get_current_active_user(
        current_user: User = Depends(get_current_user),
) -> User:
    """
    获取当前激活用户
    """
    if not current_user.is_active:
        raise HTTPException(status_code=403, detail="用户未激活")
    return current_user


async def get_current_active_user_async(
        current_user: User = Depends(get_current_user_async),
) -> User:
    """
    异步获取当前激活用户
    """
    if not current_user.is_active:
        raise HTTPException(status_code=403, detail="用户未激活")
    return current_user


def _ensure_manage_user(current_user: User) -> User:
    """
    校验用户具备全局管理权限。
    """
    permissions = current_user.permissions or {}
    if not current_user.is_superuser and not bool(permissions.get("manage")):
        raise HTTPException(
            status_code=400, detail="用户权限不足"
        )
    return current_user


def get_current_active_manage_user(
        current_user: User = Depends(get_current_active_user),
) -> User:
    """
    获取当前拥有管理权限的激活用户。
    """
    return _ensure_manage_user(current_user)


async def get_current_active_manage_user_async(
        current_user: User = Depends(get_current_active_user_async),
) -> User:
    """
    异步获取当前拥有管理权限的激活用户。
    """
    return _ensure_manage_user(current_user)


def get_current_active_superuser(
        current_user: User = Depends(get_current_user),
) -> User:
    """
    获取当前激活超级管理员
    """
    if not current_user.is_superuser:
        raise HTTPException(
            status_code=400, detail="用户权限不足"
        )
    return current_user


async def get_current_active_superuser_async(
        current_user: User = Depends(get_current_user_async),
) -> User:
    """
    异步获取当前激活超级管理员
    """
    if not current_user.is_superuser:
        raise HTTPException(
            status_code=400, detail="用户权限不足"
        )
    return current_user
