from datetime import timedelta
from typing import Any, List, Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response
from fastapi.security import OAuth2PasswordRequestForm
from fastapi.responses import JSONResponse

from app import schemas
from app.chain.user import MfaRequired, UserChain
from app.core import security
from app.core.config import settings
from app.db.systemconfig_oper import SystemConfigOper
from app.helper.sites import SitesHelper  # noqa
from app.helper.image import WallpaperHelper
from app.schemas.types import SystemConfigKey

router = APIRouter()


@router.post("/access-token", summary="获取token", response_model=schemas.Token)
def login_access_token(
    request: Request,
    response: Response,
    form_data: Annotated[OAuth2PasswordRequestForm, Depends()],
    otp_password: Annotated[str | None, Form()] = None,
) -> Any:
    """
    获取认证Token
    """
    success, user_or_message = UserChain().user_authenticate(
        username=form_data.username, password=form_data.password, mfa_code=otp_password
    )

    if not success:
        # 只有密码已经验证通过时才返回 MFA 方法，避免泄露账号安全配置。
        if isinstance(user_or_message, MfaRequired):
            return JSONResponse(
                status_code=401,
                content={
                    "detail": "需要二次验证",
                    "mfa_methods": list(user_or_message.methods),
                },
                headers={"X-MFA-Required": "true"},
            )
        raise HTTPException(status_code=401, detail="用户名或密码错误")

    # 用户等级
    level = SitesHelper().auth_level
    # 是否显示配置向导
    show_wizard = (
        not SystemConfigOper().get(SystemConfigKey.SetupWizardState)
        and not settings.ADVANCED_MODE
    )
    access_token = security.create_access_token(
        userid=user_or_message.id,
        username=user_or_message.name,
        super_user=user_or_message.is_superuser,
        expires_delta=timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
        level=level,
    )
    security.set_or_refresh_resource_token_cookie(
        request,
        response,
        schemas.TokenPayload(
            sub=user_or_message.id,
            username=user_or_message.name,
            super_user=user_or_message.is_superuser,
            level=level,
            purpose="authentication",
        ),
    )

    return schemas.Token(
        access_token=access_token,
        token_type="bearer",
        super_user=user_or_message.is_superuser,
        user_id=user_or_message.id,
        user_name=user_or_message.name,
        avatar=user_or_message.avatar,
        level=level,
        permissions=user_or_message.permissions or {},
        wizard=show_wizard,
    )


@router.get("/wallpaper", summary="登录页面电影海报", response_model=schemas.Response)
def wallpaper() -> Any:
    """
    获取登录页面电影海报
    """
    url = WallpaperHelper().get_wallpaper()
    if url:
        return schemas.Response(success=True, message=url)
    return schemas.Response(success=False)


@router.get("/wallpapers", summary="登录页面电影海报列表", response_model=List[str])
def wallpapers() -> Any:
    """
    获取登录页面电影海报
    """
    return WallpaperHelper().get_wallpapers()
