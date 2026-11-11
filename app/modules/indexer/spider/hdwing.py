# -*- coding: utf-8 -*-
"""
补全 HDWinG 种子列表促销标识的爬虫解析前处理。

HDWinG 的种子列表不在 HTML 中渲染 Free/2xFree 等促销标识，仅输出按种子 ID
标记的占位符，页面加载完成后由前端脚本批量请求 ajax_promotion.php 取回每条
种子的促销状态再填充页面。本模块提供在通用选择器解析前模拟同一请求、把返回
的促销标识注入占位符位置的混入，使标准 NexusPHP 选择器模板（img.pro_free 等）
可以正常识别优惠信息。

模块不得导入 ``app.modules.indexer`` 子树内的任何模块：宿主包会加载本模块，
反向导入会形成未审查的静态依赖环。
"""

import re
from typing import Any, Optional
from urllib.parse import urljoin

from app.adapters.network.http import RequestUtils
from app.runtime.log import logger

# 等待促销状态的占位符，例如 <span class="sp_state_placeholder" id="838806"></span>。
_PLACEHOLDER_PATTERN = re.compile(
    r'<span[^>]*class="[^"]*sp_state_placeholder[^"]*"[^>]*id="(\d+)"[^>]*>\s*</span>'
)

# CSRF 令牌声明：优先读取 meta 声明，回退到退出链接参数。
_CSRF_META_PATTERN = re.compile(r'name="x-csrf"[^>]*content="([0-9a-zA-Z]+)"')
_CSRF_LINK_PATTERN = re.compile(r'logout\.php\?csrf=([0-9a-zA-Z]+)')

# 促销状态查询接口与列表页路径，相对站点根地址解析。
_PROMOTION_PAGE = "ajax_promotion.php"
_TORRENTS_PAGE = "torrents.php"


def _extract_csrf(html_text: str) -> Optional[str]:
    """
    从页面文本中提取请求促销接口所需的 CSRF 令牌。

    :param html_text: 已解码的站点响应正文
    :return: CSRF 令牌；页面未声明时为 None
    """
    match = _CSRF_META_PATTERN.search(html_text) or _CSRF_LINK_PATTERN.search(html_text)
    return match.group(1) if match else None


class HDWingPromotionMixin:
    """
    为站点爬虫提供解析前补全 HDWinG 促销标识的能力。

    依赖组合方（SiteSpider 子类）提供的请求上下文属性，组合方在覆盖 ``parse``
    时先调用 ``inject_promotions`` 再执行通用解析。

    :ivar domain: 站点根地址，以斜杠结尾
    :ivar ua: 请求 User-Agent
    :ivar cookie: 站点 Cookie 文本
    :ivar proxies: 站点代理配置；未启用代理时为 None
    :ivar _timeout: 单次请求超时秒数
    :ivar indexername: 站点名称，用于日志标识
    """

    # 组合方必须提供的请求上下文，仅作类型声明，不持有状态。
    domain: Optional[str]
    ua: Optional[str]
    cookie: Optional[str]
    proxies: Any
    _timeout: int
    indexername: Optional[str]

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
            lambda match: promotion_html.get(match.group(1), match.group(0)),
            html_text,
        )

    def __fetch_promotion_html(self, html_text: str) -> dict[str, str]:
        """
        批量请求促销状态接口，返回种子 ID 到促销标识 HTML 的映射。

        :param html_text: 已解码的站点响应正文
        :return: 种子 ID 到 `<p>` 包裹的促销标识与限时文本拼接结果的映射；请求失败时为空字典
        """
        torrent_ids = list(dict.fromkeys(_PLACEHOLDER_PATTERN.findall(html_text)))
        csrf = _extract_csrf(html_text)
        if not torrent_ids or not csrf:
            logger.warn(f"{self.indexername} 页面缺少促销占位符或 CSRF 令牌，跳过促销状态补全")
            return {}
        domain = self.domain or ""
        res = RequestUtils(
            ua=self.ua,
            cookies=self.cookie,
            timeout=self._timeout,
            referer=urljoin(domain, _TORRENTS_PAGE),
            proxies=self.proxies,
        ).post_res(
            url=urljoin(domain, _PROMOTION_PAGE),
            data={"ids[]": torrent_ids, "csrf": csrf},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        if res is None or res.status_code != 200:
            logger.warn(
                f"{self.indexername} 促销状态请求失败，HTTP {getattr(res, 'status_code', '无响应')}"
            )
            return {}
        try:
            result = res.json()
        except ValueError:
            logger.warn(f"{self.indexername} 促销状态响应不是有效 JSON，跳过促销状态补全")
            return {}
        if result.get("status") != 200:
            logger.warn(f"{self.indexername} 促销状态接口返回异常：{result.get('message')}")
            return {}
        promotion_html: dict[str, str] = {}
        for torrent_id, state in (result.get("message") or {}).items():
            state = state or {}
            sp_state = str(state.get("sp_state") or "")
            timeout = str(state.get("timeout") or "")
            if sp_state or timeout:
                # 与站点前端脚本一致，用段落包裹标识并追加限时文本。
                promotion_html[str(torrent_id)] = f"<p>{sp_state}</p>{timeout}"
        return promotion_html
