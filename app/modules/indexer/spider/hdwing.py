# -*- coding: utf-8 -*-
"""
补全 HDWinG 种子列表促销标识的爬虫解析前处理。

HDWinG 的种子列表不在 HTML 中渲染 Free/2xFree 等促销标识，仅输出按种子 ID
标记的占位符，页面加载完成后由前端脚本批量请求 ajax_promotion.php 取回每条
种子的促销状态再填充页面。本模块提供在通用选择器解析前模拟同一请求、把返回
的促销标识注入占位符位置的混入，使标准 NexusPHP 选择器模板（img.pro_free 等）
可以正常识别优惠信息。

本模块不反向导入索引器；宿主负责将补全能力与通用页面爬虫组合。
"""

import re
from typing import Any, Optional
from urllib.parse import urljoin

from app.adapters.network.http import RequestUtils
from app.application.site.throttle import defer_site_request, wait_for_site_request
from app.runtime.log import logger

# 兼容属性顺序和单、双引号，保留原始 span 供站点选择器使用。
_PLACEHOLDER_PATTERN = re.compile(
    r"""(?P<opening><span\b(?=[^>]*\sclass\s*=\s*(['"])(?:[^'"]*\s)?sp_state_placeholder"""
    r"""(?:\s[^'"]*)?\2)(?=[^>]*\sid\s*=\s*(['"])(?P<id>\d+)\3)[^>]*>)\s*(?P<closing></span\s*>)""",
    re.IGNORECASE,
)

# CSRF 令牌声明：优先读取 meta 声明，回退到退出链接参数。
_CSRF_META_PATTERN = re.compile(
    r"""<meta\b(?=[^>]*\sname\s*=\s*(['"])x-csrf\1)"""
    r"""(?=[^>]*\scontent\s*=\s*(['"])(?P<token>[^'"]+)\2)[^>]*>""",
    re.IGNORECASE,
)
_CSRF_LINK_PATTERN = re.compile(r"""logout\.php\?[^'"<>]*?\bcsrf=([\w-]+)""", re.IGNORECASE)

# 促销状态查询接口与列表页路径，相对站点根地址解析。
_PROMOTION_PAGE = "ajax_promotion.php"
_TORRENTS_PAGE = "torrents.php"


def _extract_csrf(html_text: str) -> Optional[str]:
    """
    从页面文本中提取请求促销接口所需的 CSRF 令牌。

    :param html_text: 已解码的站点响应正文
    :return: CSRF 令牌；页面未声明时为 None
    """
    meta = _CSRF_META_PATTERN.search(html_text)
    if meta:
        return meta.group("token")
    link = _CSRF_LINK_PATTERN.search(html_text)
    return link.group(1) if link else None


class HDWingPromotionMixin:
    """
    为站点爬虫提供解析前补全 HDWinG 促销标识的能力。

    依赖组合方提供的请求上下文，组合方在通用页面解析前调用促销补全。

    :ivar domain: 站点根地址，以斜杠结尾
    :ivar ua: 请求 User-Agent
    :ivar cookie: 站点 Cookie 文本
    :ivar proxies: 站点代理配置；未启用代理时为 None
    :ivar _timeout: 单次请求超时秒数
    :ivar indexername: 站点名称，用于日志标识
    :ivar _promotion_site: 与搜索共用的站点流控配置，不包含认证凭据
    """

    # 组合方必须提供的请求上下文，仅作类型声明，不持有状态。
    domain: Optional[str]
    ua: Optional[str]
    cookie: Optional[str]
    proxies: Any
    _timeout: int
    indexername: Optional[str]
    _promotion_site: dict[str, object]

    def inject_promotions(self, html_text: str) -> str:
        """
        把促销接口返回的标识填充到页面占位符位置。

        页面没有占位符时不发起额外请求；促销接口失败时按无促销的原始页面解析，
        只损失优惠识别能力，不影响种子列表本身的获取。

        :param html_text: 已解码的站点响应正文
        :return: 已注入促销标识的页面文本；无法补全时为原始文本
        """
        if "sp_state_placeholder" not in html_text:
            return html_text
        try:
            promotion_html = self.__fetch_promotion_html(html_text)
        except Exception as err:
            logger.warn(f"{self.indexername} 促销状态补全失败，按无促销页面解析：{str(err)}")
            return html_text
        if not promotion_html:
            return html_text
        return _PLACEHOLDER_PATTERN.sub(
            lambda match: (
                f"{match.group('opening')}{promotion_html[match.group('id')]}{match.group('closing')}"
                if match.group("id") in promotion_html else match.group(0)
            ),
            html_text,
        )

    def __fetch_promotion_html(self, html_text: str) -> dict[str, str]:
        """
        批量请求促销状态接口，返回种子 ID 到促销标识 HTML 的映射。

        :param html_text: 已解码的站点响应正文
        :return: 种子 ID 到 `<p>` 包裹的促销标识与限时文本拼接结果的映射；请求失败时为空字典
        """
        torrent_ids = list(dict.fromkeys(match.group("id") for match in _PLACEHOLDER_PATTERN.finditer(html_text)))
        csrf = _extract_csrf(html_text)
        if not torrent_ids or not csrf:
            logger.warn(f"{self.indexername} 页面缺少促销占位符或 CSRF 令牌，跳过促销状态补全")
            return {}
        domain = self.domain or ""
        # 列表页和促销接口是两次实际请求，必须申请同一站点的共享额度。
        wait_for_site_request(self._promotion_site)
        client = RequestUtils(
            ua=self.ua,
            cookies=self.cookie,
            timeout=self._timeout,
            referer=urljoin(domain, _TORRENTS_PAGE),
            proxies=self.proxies,
        )
        # post_res 的 headers 参数会替换默认请求头，需先合并以保留 UA 和 Referer。
        client.update_headers({"X-Requested-With": "XMLHttpRequest"})
        res = client.post_res(
            url=urljoin(domain, _PROMOTION_PAGE),
            data={"ids[]": torrent_ids, "csrf": csrf},
        )
        if res is None or res.status_code != 200:
            if res is not None and res.status_code == 429:
                # 促销接口触发频控时，同站点的后续搜索和取种也需进入冷却。
                defer_site_request(self._promotion_site, "触发站点流控，请稍后重试")
            logger.warn(
                f"{self.indexername} 促销状态请求失败，HTTP {getattr(res, 'status_code', '无响应')}"
            )
            return {}
        try:
            result = res.json()
        except ValueError:
            logger.warn(f"{self.indexername} 促销状态响应不是有效 JSON，跳过促销状态补全")
            return {}
        if not isinstance(result, dict):
            logger.warn(f"{self.indexername} 促销状态响应结构异常，跳过促销状态补全")
            return {}
        if result.get("status") != 200:
            logger.warn(f"{self.indexername} 促销状态接口返回异常：{result.get('message')}")
            return {}
        states = result.get("message") or {}
        if not isinstance(states, dict):
            logger.warn(f"{self.indexername} 促销状态列表结构异常，跳过促销状态补全")
            return {}
        promotion_html: dict[str, str] = {}
        for torrent_id, state in states.items():
            state = state or {}
            if not isinstance(state, dict):
                logger.warn(f"{self.indexername} 种子 {torrent_id} 的促销状态结构异常，跳过促销状态补全")
                return {}
            sp_state = str(state.get("sp_state") or "")
            timeout = str(state.get("timeout") or "")
            if sp_state or timeout:
                # 与站点前端脚本一致，用段落包裹标识并追加限时文本。
                promotion_html[str(torrent_id)] = f"<p>{sp_state}</p>{timeout}"
        return promotion_html
