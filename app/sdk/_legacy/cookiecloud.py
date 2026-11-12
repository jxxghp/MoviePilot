"""
保留旧 CookieCloud 插件入口，通过注入的 Module 域名能力维持站点归组语义。
"""

from app.adapters.external.cookiecloud import CookieCloudHelper as CookieCloudAdapter
from app.application.site.protocol import site_domain


class CookieCloudHelper(CookieCloudAdapter):
    """
    旧插件无参 CookieCloud 门面，不在技术 Adapter 复制专站域名表。
    """

    def __init__(self) -> None:
        """
        沿用当前站点 Module 的域名归组规则。
        """
        super().__init__(domain_resolver=site_domain)


__all__ = ["CookieCloudHelper"]
