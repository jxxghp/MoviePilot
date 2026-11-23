"""
旧站点地址与页面规则的精确兼容门面，不扩大站点 DB Oper 的导出范围。
"""

from app.application.site.protocol import site_domain as extract_domain
from app.domain.site import SiteUtils, resolve_page_url, urls_match

__all__ = ["SiteUtils", "extract_domain", "resolve_page_url", "urls_match"]
