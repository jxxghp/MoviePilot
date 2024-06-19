from datetime import timedelta
from typing import Any, List

from fastapi import APIRouter, Depends, HTTPException, Form
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
from app.helper.sites import SitesHelper
from app.log import logger
from app.utils.web import WebUtils

router = APIRouter()


@router.post("/access-token", summary="获取token", response_model=schemas.Token)
async def login_access_token(
        db: Session = Depends(get_db),
        form_data: OAuth2PasswordRequestForm = Depends(),
        otp_password: str = Form(None)
) -> Any:
    """
    获取认证Token
    """
    # 检查数据库
    success, user = User.authenticate(
        db=db,
        name=form_data.username,
        password=form_data.password,
        otp_password=otp_password
    )
    if not success:
        # 认证不成功
        if not user:
            # 未找到用户，请求协助认证
            logger.warn(f"登录用户 {form_data.username} 本地不存在，尝试辅助认证 ...")
            token = UserChain().user_authenticate(form_data.username, form_data.password)
            if not token:
                logger.warn(f"用户 {form_data.username} 登录失败！")
                raise HTTPException(status_code=401, detail="用户名、密码、二次校验码不正确")
            else:
                logger.info(f"用户 {form_data.username} 辅助认证成功，用户信息: {token}，以普通用户登录...")
                # 加入用户信息表
                logger.info(f"创建用户: {form_data.username}")
                user = User(name=form_data.username, is_active=True,
                            is_superuser=False, hashed_password=get_password_hash(token))
                user.create(db)
        else:
            # 用户存在，但认证失败
            logger.warn(f"用户 {user.name} 登录失败！")
            raise HTTPException(status_code=401, detail="用户名、密码或二次校验码不正确")
    elif user and not user.is_active:
        raise HTTPException(status_code=403, detail="用户未启用")
    logger.info(f"用户 {user.name} 登录成功！")
    level = SitesHelper().auth_level
    return schemas.Token(
        access_token=security.create_access_token(
            userid=user.id,
            username=user.name,
            super_user=user.is_superuser,
            expires_delta=timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
            level=level
        ),
        token_type="bearer",
        super_user=user.is_superuser,
        user_name=user.name,
        avatar=user.avatar,
        level=level
    )


@router.get("/wallpaper", summary="登录页面电影海报", response_model=schemas.Response)
def wallpaper() -> Any:
    """
    获取登录页面电影海报
    """
    if settings.WALLPAPER == "tmdb":
        url = TmdbChain().get_random_wallpager()
    else:
        url = WebUtils.get_bing_wallpaper()
    if url:
        return schemas.Response(
            success=True,
            message=url
        )
    return schemas.Response(success=False)


@router.get("/wallpapers", summary="登录页面电影海报列表", response_model=List[str])
def wallpapers() -> Any:
    """
    获取登录页面电影海报
    """
    if settings.WALLPAPER == "tmdb":
        return TmdbChain().get_trending_wallpapers()
    else:
        return WebUtils.get_bing_wallpapers()
