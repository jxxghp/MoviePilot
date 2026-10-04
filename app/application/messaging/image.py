"""消息渠道使用的外部图片地址转换与签名能力。"""

from __future__ import annotations

from typing import Optional
from urllib.parse import urlparse

from app.application.security.url import SecurityUtils
from app.foundation.url import UrlUtils
from app.runtime.settings import get_runtime_setting

WECHAT_IMAGE_SIGNATURE_PURPOSE = "wechat-notification-image"
_WECHAT_IMAGE_PROXY_PATH = "/system/notification-image"


def _is_douban_image_url(image_url: str) -> bool:
    """判断地址是否来自豆瓣图片 CDN，避免公开代理暴露其他资源。"""
    hostname = (urlparse(image_url).hostname or "").lower().rstrip(".")
    return hostname == "doubanio.com" or hostname.endswith(".doubanio.com")


def build_wechat_image_url(image_url: Optional[str]) -> Optional[str]:
    """
    为企业微信通知图片生成可由微信服务器访问的签名代理地址。

    企业微信服务器不会携带 MoviePilot 的登录 Cookie，只有配置了对外域名时
    才能访问本地代理；未配置时保留原始地址，避免改变现有通知行为。
    """
    normalized_url = (image_url or "").strip()
    if not normalized_url:
        return image_url

    parsed_url = urlparse(normalized_url)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        return image_url
    if not _is_douban_image_url(normalized_url):
        return image_url

    try:
        app_domain = str(get_runtime_setting("APP_DOMAIN") or "").strip()
        api_v1_path = str(get_runtime_setting("API_V1_STR") or "/api/v1").rstrip("/")
    except AttributeError:
        return image_url
    if not app_domain:
        return image_url

    signed_url = SecurityUtils.sign_url(
        normalized_url,
        purpose=WECHAT_IMAGE_SIGNATURE_PURPOSE,
    )
    proxy_url = UrlUtils.combine_url(
        host=app_domain,
        path=f"{api_v1_path}{_WECHAT_IMAGE_PROXY_PATH}",
        query={"url": signed_url},
    )
    return proxy_url or image_url


def verify_wechat_image_url(image_url: str) -> Optional[str]:
    """验证企业微信豆瓣图片代理地址并返回去签名的原始图片地址。"""
    source_url = SecurityUtils.verify_signed_url(
        image_url,
        purpose=WECHAT_IMAGE_SIGNATURE_PURPOSE,
    )
    return source_url if source_url and _is_douban_image_url(source_url) else None
