from datetime import timedelta
from typing import Annotated, Any, List
from urllib.parse import quote, urlparse, urlunparse

from fastapi import APIRouter, Depends, Form, Header, HTTPException, Request, Response
from fastapi.security import OAuth2PasswordRequestForm
from fastapi.responses import JSONResponse

from app import schemas
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
_LOGIN_WALLPAPER_PUBLIC_PURPOSE = "login-wallpaper-public"
_LOGIN_WALLPAPER_MEDIA_PURPOSE = "login-wallpaper-media"


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
def wallpapers(same_origin: bool = False) -> Any:
    """
    获取登录页面电影海报。

    默认保持外链列表合同；同源模式只对绝对 HTTP(S) 地址做一对一签名转换，不改变
    来源数量、顺序、重复项或相对地址。
    """
    wallpaper_urls = WallpaperHelper().get_wallpapers()
    if not same_origin:
        return wallpaper_urls

    purpose = (
        _LOGIN_WALLPAPER_MEDIA_PURPOSE
        if settings.WALLPAPER == "mediaserver"
        else _LOGIN_WALLPAPER_PUBLIC_PURPOSE
    )
    return [_login_wallpaper_proxy_url(url, purpose) for url in wallpaper_urls]


def _login_wallpaper_proxy_url(url: str, purpose: str) -> str:
    """
    将可代理的绝对壁纸地址转换为登录页专用同源签名地址。

    `//cdn.example/one.jpg` 这类网络路径引用会被浏览器解析成跨源地址，因此按
    当前页面协议补全后同样走代理；只有不带 netloc 的相对地址才原样返回。
    """
    parsed = urlparse(url)
    if not parsed.netloc:
        return url
    if not parsed.scheme:
        url = urlunparse(parsed._replace(scheme="https"))
    elif parsed.scheme not in {"http", "https"}:
        return url
    signed_url = SecurityUtils.sign_url(url, purpose=purpose)
    return (
        f"{settings.API_V1_STR}/login/wallpapers/image"
        f"?url={quote(signed_url, safe='')}"
    )


def _url_origin(url: str) -> tuple[str, str, int | None] | None:
    """返回 HTTP(S) 地址的规范 origin，拒绝缺少主机或携带用户信息的地址。"""
    try:
        parsed = urlparse(url)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        return None
    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    return parsed.scheme, parsed.hostname.lower(), port


@router.get("/wallpapers/image", summary="登录页面同源壁纸")
async def wallpaper_image(
    url: str,
    if_none_match: Annotated[str | None, Header()] = None,
) -> Response:
    """
    读取后端壁纸来源签发的图片；客户端无法修改目标后继续复用签名。

    响应带内容 ETag，缓存过期后条件请求命中时只返回 304。
    """
    source_url = SecurityUtils.verify_signed_url(
        url, purpose=_LOGIN_WALLPAPER_PUBLIC_PURPOSE
    )
    media_source = False
    if not source_url:
        source_url = SecurityUtils.verify_signed_url(
            url, purpose=_LOGIN_WALLPAPER_MEDIA_PURPOSE
        )
        media_source = bool(source_url)
    source_origin = _url_origin(source_url or "")
    if not source_url or not source_origin:
        raise HTTPException(status_code=404, detail="Wallpaper not found")

    async def is_safe_public_target(target_url: str) -> bool:
        """自定义公共来源可跨域跳转，但每个目标都必须通过 DNS/私网校验。"""
        target_origin = _url_origin(target_url)
        if not target_origin:
            return False
        return await SecurityUtils.is_safe_image_url_async(
            target_url,
            {target_origin[1]},
            allowed_private_ranges=settings.IMAGE_PROXY_ALLOWED_PRIVATE_RANGES,
        )

    async def is_safe_redirect(target_url: str) -> bool:
        """媒体服务器签名只授权原 origin；其它跳转按公共图片目标重新校验。"""
        target_origin = _url_origin(target_url)
        if media_source and target_origin == source_origin:
            return True
        return await is_safe_public_target(target_url)

    if not media_source and not await is_safe_public_target(source_url):
        raise HTTPException(status_code=404, detail="Wallpaper not found")

    # 媒体服务器来源额外继承原 origin 的私网授权，与公共来源的重定向授权范围不同，
    # 因此按来源类型声明策略标识，只让同策略的并发请求共享一次抓取。
    redirect_policy = (
        f"{_LOGIN_WALLPAPER_MEDIA_PURPOSE}:{source_origin[0]}://"
        f"{source_origin[1]}:{source_origin[2]}"
        if media_source
        else _LOGIN_WALLPAPER_PUBLIC_PURPOSE
    )
    content = await ImageHelper().async_fetch_image_guarded(
        url=source_url,
        redirect_validator=is_safe_redirect,
        redirect_policy=redirect_policy,
        max_bytes=_LOGIN_WALLPAPER_MAX_BYTES,
        use_cache=True,
    )
    if not content:
        raise HTTPException(status_code=502, detail="Wallpaper unavailable")

    etag = f'"{HashUtils.md5(content)}"'
    headers = RequestUtils.generate_cache_headers(etag, max_age=86400)
    if RequestUtils.if_none_match_matches(if_none_match, etag):
        return Response(status_code=304, headers=headers)

    return Response(
        content=content,
        media_type=UrlUtils.get_mime_type(source_url, "image/jpeg"),
        headers=headers,
    )
