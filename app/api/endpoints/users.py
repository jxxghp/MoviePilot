from typing import Any, List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app import schemas
from app.core.security import get_password_hash
from app.db import get_db
from app.db.models.user import User
from app.db.userauth import get_current_active_superuser, get_current_active_user

router = APIRouter()


@router.get("/", response_model=List[schemas.User])
async def read_users(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_superuser),
) -> Any:
    """
    查询用户列表
    """
    users = current_user.list(db)
    return users


@router.post("/", response_model=schemas.User)
async def create_user(
    *,
    db: Session = Depends(get_db),
    user_in: schemas.UserCreate,
    current_user: User = Depends(get_current_active_superuser),
) -> Any:
    """
    新增用户
    """
    user = current_user.get_by_email(db, email=user_in.email)
    if user:
        raise HTTPException(
            status_code=400,
            detail="用户已存在",
        )
    user_info = user_in.dict()
    if user_info.get("password"):
        user_info["hashed_password"] = get_password_hash(user_info["password"])
        user_info.pop("password")
    user = User(**user_info)
    user = user.create(db)
    return user


@router.get("/{user_id}", response_model=schemas.User)
async def read_user_by_id(
    user_id: int,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
) -> Any:
    """
    查询用户详情
    """
    user = current_user.get(db, rid=user_id)
    if not user:
        raise HTTPException(
            status_code=404,
            detail="用户不存在",
        )
    if user == current_user:
        return user
    if not user.is_superuser:
        raise HTTPException(
            status_code=400,
            detail="用户权限不足"
        )
    return user
