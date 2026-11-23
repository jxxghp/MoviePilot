"""
浏览器推送持久化服务的显式启动与撤销组合根。
"""

from functools import partial
from urllib.parse import urlparse

from app.adapters.network.webpush import configure_webpush_endpoint_validator
from app.application.security.url import SecurityUtils
from app.application.webpush import WebPushService, configure_webpush_service
from app.db.adapters.webpush import webpush_scope
from app.db.session import SessionFactory


def validate_webpush_origin(origin: str) -> bool:
    """
    为推送协议层装配既有公网 DNS 策略，不传递含能力令牌的完整 URL。

    :param origin: 仅含 HTTPS 协议、域名和端口的推送源站
    :return: 所有解析结果均为公网地址时为真；未装配 DNS 时默认拒绝
    """
    return SecurityUtils.is_safe_url(origin, {urlparse(origin).netloc}, strict=True, block_private=True)


def configure_webpush_services() -> None:
    """
    将独占数据库事务工厂注入浏览器推送应用服务。
    """
    configure_webpush_endpoint_validator(validate_webpush_origin)
    configure_webpush_service(WebPushService(partial(webpush_scope, SessionFactory)))


def reset_webpush_services() -> None:
    """
    撤销当前启动周期的服务，禁止关停后回退到历史无归属订阅。
    """
    configure_webpush_service(None)
    configure_webpush_endpoint_validator(None)
