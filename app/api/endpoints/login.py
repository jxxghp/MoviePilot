from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session

from app import schemas
from app.chain.tmdb import TmdbChain
from app.chain.user import UserChain
from app.core import security
from app.core.config import settings
from app.core.security import get_password_hash
from app.db import get_db
from app.db.models.user import User
from app.log import logger
from app.utils.web import WebUtils

router = APIRouter()


@router.post("/access-token", summary="获取token", response_model=schemas.Token)
async def login_access_token(
        db: Session = Depends(get_db), form_data: OAuth2PasswordRequestForm = Depends()
) -> Any:
    """
    获取认证Token
    """
    # 检查数据库
    user = User.authenticate(
        db=db,
        name=form_data.username,
        password=form_data.password
    )
    if not user:
        # 请求协助认证
        logger.warn("登录用户本地不匹配，尝试辅助认证 ...")
        token = UserChain().user_authenticate(form_data.username, form_data.password)
        if not token:
            logger.warn(f"用户 {form_data.username} 登录失败！")
            raise HTTPException(status_code=401, detail="用户名或密码不正确")
        else:
            logger.info(f"用户 {form_data.username} 辅助认证成功，用户信息: {token}")
            # 加入用户信息表
            user = User.get_by_name(db=db, name=form_data.username)
            if not user:
                logger.info(f"用户不存在，创建普通用户: {form_data.username}")
                user = User(name=form_data.username, is_active=True,
                            is_superuser=False, hashed_password=get_password_hash(token))
                user.create(db)
            else:
                # 普通用户权限
                user.is_superuser = False
    elif not user.is_active:
        raise HTTPException(status_code=403, detail="用户未启用")
    logger.info(f"用户 {user.name} 登录成功！")
    return schemas.Token(
        access_token=security.create_access_token(
            user.id,
            expires_delta=timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
        ),
        token_type="bearer",
        super_user=user.is_superuser,
        user_name=user.name,
        avatar=user.avatar
    )


@router.get("/wallpaper", summary="登录页面电影海报", response_model=schemas.Response)
def wallpaper() -> Any:
    """
    获取登录页面电影海报
    """
    if settings.WALLPAPER == "tmdb":
        return tmdb_wallpaper()
    elif settings.WALLPAPER == "bing":
        return bing_wallpaper()
    return schemas.Response(success=False)


@router.get("/bing", summary="Bing每日壁纸", response_model=schemas.Response)
def bing_wallpaper() -> Any:
    """
    获取Bing每日壁纸
    """
    url = WebUtils.get_bing_wallpaper()
    if url:
        return schemas.Response(
            success=True,
            message=url
        )
    return schemas.Response(success=False)


@router.get("/tmdb", summary="TMDB电影海报", response_model=schemas.Response)
def tmdb_wallpaper() -> Any:
    """
    获取TMDB电影海报
    """
    wallpager = TmdbChain().get_random_wallpager()
    if wallpager:
        return schemas.Response(
            success=True,
            message=wallpager
        )
    return schemas.Response(success=False)
