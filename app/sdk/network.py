"""插件常用的 HTTP、地址和站点处理工具。

``UrlSafetyDiagnosis`` 是 ``SecurityUtils.evaluate_url_safety()`` 的返回内容，
``UrlSafetyReason`` 是它 ``reason`` 字段的取值域——判定被拒的地址栽在哪一条上要读这个枚举。
"""

from app.domain.site import SiteUtils, extract_domain, urls_match
from app.adapters.network.ip import IpUtils
from app.foundation.url import (
    UrlUtils,
    base_url,
    host_label,
    is_link,
    parse_address,
    sanitize_path,
    second_level_label,
    split_netloc,
)
from app.adapters.network.http import AsyncRequestUtils, RequestUtils, cookie_parse
from app.adapters.external.location import WebUtils
from app.application.rss import RssHelper
from app.adapters.network.urlsafety import (
    SecurityUtils,
    UrlSafetyDiagnosis,
    UrlSafetyReason,
)
from app.application.site.sites import SitesHelper  # pylint: disable=import-error,no-name-in-module


__all__ = [
    "AsyncRequestUtils",
    "IpUtils",
    "RequestUtils",
    "RssHelper",
    "SecurityUtils",
    "SiteUtils",
    "SitesHelper",
    "UrlSafetyDiagnosis",
    "UrlSafetyReason",
    "UrlUtils",
    "WebUtils",
    "base_url",
    "cookie_parse",
    "extract_domain",
    "host_label",
    "is_link",
    "parse_address",
    "sanitize_path",
    "second_level_label",
    "split_netloc",
    "urls_match",
]
