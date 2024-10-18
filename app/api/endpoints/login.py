from datetime import timedelta
from typing import Any, List

from fastapi import APIRouter, Depends, Form, HTTPException
from fastapi.security import OAuth2PasswordRequestForm

from app import schemas
from app.chain.tmdb import TmdbChain
from app.chain.user import UserChain
from app.core import security
from app.core.config import settings
from app.helper.sites import SitesHelper
from app.utils.web import WebUtils

router = APIRouter()


@router.post("/access-token", summary="获取token", response_model=schemas.Token)
async def login_access_token(
        form_data: OAuth2PasswordRequestForm = Depends(),
        otp_password: str = Form(None)
) -> Any:
    """
    获取认证Token
    """
    success, user_or_message = UserChain().user_authenticate(username=form_data.username,
                                                             password=form_data.password,
                                                             mfa_code=otp_password)

    if not success:
        raise HTTPException(status_code=401, detail=user_or_message)

    level = SitesHelper().auth_level
    return schemas.Token(
        access_token=security.create_access_token(
            userid=user_or_message.id,
            username=user_or_message.name,
            super_user=user_or_message.is_superuser,
            expires_delta=timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
            level=level
        ),
        token_type="bearer",
        super_user=user_or_message.is_superuser,
        user_name=user_or_message.name,
        avatar=user_or_message.avatar,
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
