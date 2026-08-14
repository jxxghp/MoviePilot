"""
API 层的公共依赖。

这些是 FastAPI 的路由依赖：从令牌解出用户、校验激活状态与权限，失败一律以
HTTPException 表达。它们此前住在 app/db/oper/user.py 里，与数据访问混在一处——
鉴权是 HTTP 层的关注点，产出的是 403/400 而不是数据。放在 db 包里既让数据层反向
依赖了 fastapi，也使这部分逻辑无法与数据访问分开度量。
"""
from fastapi import Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app import schemas
from app.application.security.access import verify_token
from app.db import get_async_db, get_db
from app.db.models.user import User


def get_current_user(
        db: Session = Depends(get_db),
        token_data: schemas.TokenPayload = Depends(verify_token)
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
        token_data: schemas.TokenPayload = Depends(verify_token)
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
