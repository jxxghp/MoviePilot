from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session

from app import schemas
from app.core import security
from app.core.config import settings
from app.db import get_db
from app.db.models.user import User

router = APIRouter()


@router.post("/login/access-token", summary="获取token", response_model=schemas.Token)
async def login_access_token(
        db: Session = Depends(get_db), form_data: OAuth2PasswordRequestForm = Depends()
) -> Any:
    """
    获取认证Token
    """
    user = User.authenticate(
        db=db,
        name=form_data.username,
        password=form_data.password
    )
    if not user:
        raise HTTPException(status_code=400, detail="用户名或密码不正确")
    elif not user.is_active:
        raise HTTPException(status_code=400, detail="用户未启用")
    access_token_expires = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    return schemas.Token(
        access_token=security.create_access_token(
            user.id, expires_delta=access_token_expires
        ),
        token_type="bearer",
    )
