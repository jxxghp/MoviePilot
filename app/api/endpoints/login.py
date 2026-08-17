from datetime import timedelta
from typing import Any, List, Annotated

from fastapi import Depends, Form, HTTPException, Request, Response
from fastapi.security import OAuth2PasswordRequestForm
from fastapi.responses import JSONResponse

from app.schemas.response import Response as _SchemaResponse
from app.schemas.token import MfaChallenge as _SchemaMfaChallenge
from app.schemas.token import Token as _SchemaToken
from app.schemas.token import TokenPayload as _SchemaTokenPayload
from app.api.response import RAW_RESPONSE_OPENAPI_KEY, ResponseAPIRouter
from app.chain.user import MfaRequired, UserChain
from app.application.security import access as security
from app.runtime.config import settings
from app.db.oper.systemconfig import SystemConfigOper
from app.application.site.sites import SitesHelper  # pylint: disable=no-name-in-module
from app.application.image import WallpaperHelper
from app.schemas.types import SystemConfigKey

router = ResponseAPIRouter()


@router.post(
    "/access-token",
    summary="获取token",
    response_model=_SchemaToken,
    responses={
        401: {
            "model": _SchemaResponse[_SchemaMfaChallenge],
            "description": "需要二次验证或认证失败",
        }
    },
    openapi_extra={RAW_RESPONSE_OPENAPI_KEY: True},
)
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
            challenge = _SchemaResponse[_SchemaMfaChallenge](
                success=False,
                message="需要二次验证",
                data=_SchemaMfaChallenge(
                    mfa_methods=list(user_or_message.methods)
                ),
            )
            return JSONResponse(
                status_code=401,
                content=challenge.model_dump(mode="json"),
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
        _SchemaTokenPayload(
            sub=user_or_message.id,
            username=user_or_message.name,
            super_user=user_or_message.is_superuser,
            level=level,
            purpose="authentication",
        ),
    )

    return _SchemaToken(
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


@router.get(
    "/wallpaper",
    summary="登录页面电影海报",
    response_model=_SchemaResponse[str],
)
def wallpaper() -> Any:
    """
    获取登录页面电影海报
    """
    url = WallpaperHelper().get_wallpaper()
    if url:
        return _SchemaResponse(success=True, data=url)
    return _SchemaResponse(success=False)


@router.get("/wallpapers", summary="登录页面电影海报列表", response_model=List[str])
def wallpapers() -> Any:
    """
    获取登录页面电影海报
    """
    return WallpaperHelper().get_wallpapers()
