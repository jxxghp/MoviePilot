import asyncio
from datetime import timedelta
from typing import Annotated, Any, List

from fastapi import Depends, Form, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from fastapi.security import OAuth2PasswordRequestForm

from app.adapters.web.security.access import set_or_refresh_resource_token_cookie
from app.api.context import get_api_runtime_config, resolve_api_runtime_config
from app.api.dependencies.auth import get_user_service
from app.api.response import (
    RAW_RESPONSE_OPENAPI_KEY,
    CompatibleCountParam,
    CompatiblePageParam,
    ResponseAPIRouter,
)
from app.application.configuration import ApiRuntimeConfig, get_runtime_settings
from app.application.image import WallpaperHelper
from app.application.security.token import PasswordTooLongError, create_access_token, get_password_hash
from app.application.security.user import UserNameConflictError, UserService
from app.application.site.sites import SitesHelper  # pylint: disable=import-error,no-name-in-module
from app.chain.user import MfaRequired, UserChain
from app.schemas.initialization import InitializationRequest as _SchemaInitializationRequest
from app.schemas.initialization import InitializationStatus as _SchemaInitializationStatus
from app.schemas.response import Response as _SchemaResponse
from app.schemas.token import MfaChallenge as _SchemaMfaChallenge
from app.schemas.token import Token as _SchemaToken
from app.schemas.token import TokenPayload as _SchemaTokenPayload

router = ResponseAPIRouter()
_INITIALIZATION_LOCK = asyncio.Lock()


@router.get(  # type: ignore[misc]
    "/initialization",
    summary="查询首次初始化状态",
    response_model=_SchemaResponse[_SchemaInitializationStatus],
)
async def get_initialization_status(
    service: UserService = Depends(get_user_service),
) -> _SchemaResponse[_SchemaInitializationStatus]:
    """返回当前实例是否已经存在用户，供启动页决定是否接管导航。"""
    return _SchemaResponse(
        success=True,
        data=_SchemaInitializationStatus(initialized=await service.is_initialized()),
    )


@router.post(  # type: ignore[misc]
    "/initialization",
    summary="完成首次初始化",
    response_model=_SchemaResponse[None],
)
async def initialize_instance(
    payload: _SchemaInitializationRequest,
    service: UserService = Depends(get_user_service),
) -> _SchemaResponse[None]:
    """原子创建首个超级管理员，并保存 API Key 供后续服务认证。"""
    async with _INITIALIZATION_LOCK:
        if await service.is_initialized():
            raise HTTPException(status_code=409, detail="系统已经完成初始化")

        runtime_settings = get_runtime_settings()
        previous_superuser = runtime_settings.get("SUPERUSER", "")
        previous_api_token = runtime_settings.get("API_TOKEN")
        updated_keys: list[str] = []
        try:
            for key, value in (("SUPERUSER", payload.username), ("API_TOKEN", payload.api_key)):
                success, message = runtime_settings.update(key, value)
                if success is False:
                    raise RuntimeError(message or f"配置项 {key} 更新失败")
                updated_keys.append(key)

            try:
                hashed_password = get_password_hash(payload.password)
            except PasswordTooLongError as error:
                raise HTTPException(status_code=422, detail=str(error)) from error

            created_user = await service.create(
                {
                    "name": payload.username,
                    "email": "admin@movie-pilot.org",
                    "hashed_password": hashed_password,
                    "is_active": True,
                    "is_superuser": True,
                    "avatar": "",
                    "is_otp": False,
                    "otp_secret": None,
                    "permissions": {},
                    "settings": {},
                }
            )
            if created_user is None:
                raise RuntimeError("管理员用户创建失败")
        except UserNameConflictError as error:
            raise HTTPException(status_code=409, detail="用户名已被使用") from error
        except HTTPException:
            for key in reversed(updated_keys):
                runtime_settings.update(
                    key,
                    previous_superuser if key == "SUPERUSER" else previous_api_token,
                )
            raise
        except Exception as error:
            for key in reversed(updated_keys):
                runtime_settings.update(
                    key,
                    previous_superuser if key == "SUPERUSER" else previous_api_token,
                )
            raise HTTPException(status_code=400, detail=str(error)) from error

    return _SchemaResponse(success=True)


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
    runtime_config: ApiRuntimeConfig = Depends(get_api_runtime_config),
) -> Any:
    """
    获取认证Token
    """
    runtime_config = resolve_api_runtime_config(runtime_config)
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
    access_token = create_access_token(
        userid=user_or_message.id,
        username=user_or_message.name,
        super_user=user_or_message.is_superuser,
        expires_delta=timedelta(minutes=runtime_config.access_token_expire_minutes),
        level=level,
    )
    set_or_refresh_resource_token_cookie(
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
def wallpapers(page: CompatiblePageParam = None, count: CompatibleCountParam = None) -> Any:
    """
    获取登录页面电影海报
    """
    return WallpaperHelper().get_wallpapers()
