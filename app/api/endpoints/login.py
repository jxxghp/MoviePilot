from datetime import timedelta
from typing import Any, List, Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Form, Header, HTTPException, Request, Response
from fastapi.security import OAuth2PasswordRequestForm
from fastapi.responses import JSONResponse

from app import schemas
from app.chain.mediaserver import MediaServerChain
from app.chain.user import MfaRequired, UserChain
from app.core import security
from app.core.config import settings
from app.db.systemconfig_oper import SystemConfigOper
from app.helper.sites import SitesHelper  # noqa
from app.helper.image import ImageHelper, WallpaperHelper
from app.schemas.types import SystemConfigKey
from app.utils.crypto import HashUtils
from app.utils.http import RequestUtils
from app.utils.security import SecurityUtils
from app.utils.url import UrlUtils

router = APIRouter()

_LOGIN_WALLPAPER_MAX_BYTES = 32 * 1024 * 1024
_LOGIN_WALLPAPER_MAX_PIXELS = 50_000_000


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
def wallpapers(background_tasks: BackgroundTasks, same_origin: bool = False) -> Any:
    """
    获取登录页面电影海报。

    默认保持外链列表合同；同源模式只返回 catalog opaque ID 对应的本地图片入口。
    """
    helper = WallpaperHelper()
    if not same_origin:
        return helper.get_wallpapers()

    wallpaper_ids = helper.get_wallpaper_catalog_ids()
    if wallpaper_ids:
        background_tasks.add_task(helper.refresh_wallpaper_catalog)
    else:
        wallpaper_ids = helper.refresh_wallpaper_catalog()
    return [f"{settings.API_V1_STR}/login/wallpapers/{item}" for item in wallpaper_ids]


@router.get("/wallpapers/{wallpaper_id}", summary="登录页面同源壁纸")
async def wallpaper_image(
    wallpaper_id: str,
    if_none_match: Annotated[str | None, Header()] = None,
) -> Response:
    """
    读取 catalog 已登记的壁纸。

    opaque ID 是未登录访问的唯一输入，原始 URL 不进入请求合同；实际抓取仍执行图片
    allowlist、DNS/私网限制、媒体服务器凭据和磁盘缓存策略。
    """
    source_url = WallpaperHelper().get_wallpaper_catalog_source(wallpaper_id)
    if not source_url:
        raise HTTPException(status_code=404, detail="Wallpaper not found")

    allowed_domains = set(settings.SECURITY_IMAGE_DOMAINS)

    async def is_safe_target(url: str) -> bool:
        """所有远端跳转都必须重新满足登录壁纸的图片代理安全边界。"""
        return await SecurityUtils.is_safe_image_url_async(
            url,
            allowed_domains,
            allowed_private_ranges=settings.IMAGE_PROXY_ALLOWED_PRIVATE_RANGES,
        )

    if not await is_safe_target(source_url):
        raise HTTPException(status_code=404, detail="Wallpaper not found")

    fetch_url = SecurityUtils.strip_url_signature(source_url)
    cookies = MediaServerChain().get_image_cookies(server=None, image_url=source_url)
    content = await ImageHelper().async_fetch_image_guarded(
        url=fetch_url,
        redirect_validator=is_safe_target,
        max_bytes=_LOGIN_WALLPAPER_MAX_BYTES,
        max_pixels=_LOGIN_WALLPAPER_MAX_PIXELS,
        use_cache=True,
        cookies=cookies,
    )
    if not content:
        raise HTTPException(status_code=502, detail="Wallpaper unavailable")

    etag = HashUtils.md5(content)
    headers = RequestUtils.generate_cache_headers(etag, max_age=86400 * 7)
    if if_none_match == etag:
        return Response(status_code=304, headers=headers)
    return Response(
        content=content,
        media_type=UrlUtils.get_mime_type(fetch_url, "image/jpeg"),
        headers=headers,
    )
