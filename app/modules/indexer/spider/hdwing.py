# -*- coding: utf-8 -*-
"""
补全 HDWinG 种子列表促销标识的爬虫解析前处理。

HDWinG 的种子列表不在 HTML 中渲染 Free/2xFree 等促销标识，仅输出按种子 ID
标记的占位符，页面加载完成后由前端脚本批量请求 ajax_promotion.php 取回每条
种子的促销状态再填充页面。本模块提供在通用选择器解析前模拟同一请求、把返回
的可见促销标识注入占位符位置的混入，使标准 NexusPHP 选择器模板可以识别优惠。

当列表页将促销查询置于 Cloudflare Turnstile 验证门控之后时，HTTP 爬虫无法完成
交互挑战，未验证请求只会收到隐藏的伪造免费标签，此时直接跳过补全并按无促销解析；
对接口返回的 HTML 片段也会先独立解析并剔除不可见节点与未闭合标签，防止污染页面。
本模块不反向导入索引器；宿主负责将补全能力与通用页面爬虫组合。
"""

import re
from typing import Any, Optional
from urllib.parse import urljoin

from lxml import etree
from lxml import html as lxml_html

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

# 页面挂载 Turnstile 门控或在脚本中先请求 ajax_turnstile.php 时，直接请求促销接口只会返回伪造蜜罐。
_TURNSTILE_GATE_PATTERN = re.compile(
    r"""<[^>]*\bid\s*=\s*(['"])turnstile_gate\1[^>]*>"""
    r"""|<script\b[^>]*>(?:(?!</script>).)*?\bajax_turnstile\.php\b""",
    re.IGNORECASE | re.DOTALL,
)

# 浏览器不作为可见正文渲染的容器标签，静态选择器若匹配其中图标会误判促销。
_INVISIBLE_TAGS = frozenset({"script", "style", "noscript", "template"})

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


def _is_turnstile_gated(html_text: str) -> bool:
    """
    判断列表页是否要求先完成 Cloudflare Turnstile 校验才能获取真实促销状态。

    :param html_text: 已解码的站点响应正文
    :return: 页面存在 Turnstile 门控容器或脚本校验入口时返回 True
    """
    return _TURNSTILE_GATE_PATTERN.search(html_text) is not None


def _has_hidden_style(style_text: str) -> bool:
    """
    判断内联 CSS 样式是否将元素声明为不可见。

    覆盖站点蜜罐常用的 display:none、visibility:hidden/collapse 与 opacity:0，
    兼容尾部附带 !important 的写法。

    :param style_text: 元素的 style 属性原始文本
    :return: 包含隐藏样式声明时返回 True
    """
    for declaration in style_text.split(";"):
        if ":" not in declaration:
            continue
        prop, raw_val = declaration.split(":", 1)
        prop_name = prop.strip().lower()
        val = raw_val.strip().lower()
        if val.endswith("!important"):
            val = val[:-10].strip()
        if prop_name == "display" and val == "none":
            return True
        if prop_name == "visibility" and val in {"hidden", "collapse"}:
            return True
        if prop_name == "opacity":
            try:
                if float(val) == 0.0:
                    return True
            except ValueError:
                continue
    return False


def _is_hidden_node(element: Any) -> bool:
    """
    判断解析后的 DOM 节点是否属于注释、非渲染容器或视觉隐藏元素。

    :param element: lxml 解析生成的节点对象
    :return: 节点及其子树不应参与促销选择器匹配时返回 True
    """
    if not isinstance(element.tag, str):
        return True
    if element.tag.lower() in _INVISIBLE_TAGS:
        return True
    if any(str(attr).lower() == "hidden" for attr in element.attrib):
        return True
    style = str(element.attrib.get("style") or "")
    return bool(style and _has_hidden_style(style))


def _sanitize_promotion_part(raw_html: str) -> str:
    """
    将单段促销 HTML 作为独立片段解析，剔除隐藏蜜罐与未闭合标签。

    站点在未通过验证或防抓取场景下会返回带 display:none 且未闭合属性引号的假免费标签；
    独立片段解析可阻止未闭合引号吞噬列表页后续单元格，移除隐藏子树后仅保留真实可见内容。

    :param raw_html: 接口返回的 sp_state 或 timeout 原始字符串
    :return: 闭合且可见的 HTML 片段；无有效可见内容或解析失败时返回空字符串
    """
    if not raw_html or not raw_html.strip():
        return ""
    try:
        wrapper = lxml_html.fragment_fromstring(f"<div>{raw_html}</div>")
    except (etree.ParserError, ValueError, TypeError):
        return ""
    for element in list(wrapper.iterdescendants()):
        if element.getparent() is None:
            continue
        if _is_hidden_node(element):
            element.drop_tree()
    if len(wrapper) == 0 and not (wrapper.text_content() or "").strip():
        return ""
    parts = [wrapper.text or ""]
    for child in wrapper:
        parts.append(lxml_html.tostring(child, encoding="unicode"))
    return "".join(parts).strip()


def _format_promotion_fragment(sp_state: str, timeout: str) -> str:
    """
    净化并组装单条种子的促销标识与限时信息片段。

    :param sp_state: 接口返回的优惠图标 HTML
    :param timeout: 接口返回的优惠剩余时间 HTML
    :return: 可注入占位符的闭合 HTML；无可见促销信息时返回空字符串
    """
    clean_state = _sanitize_promotion_html_value(sp_state)
    clean_timeout = _sanitize_promotion_html_value(timeout)
    if not clean_state and not clean_timeout:
        return ""
    # 与站点前端脚本一致，用段落包裹标识并追加限时文本。
    return f"<p>{clean_state}</p>{clean_timeout}"


def _sanitize_promotion_html_value(value: Any) -> str:
    """
    将接口字段值转为字符串并净化为可见 HTML 片段。

    :param value: 接口字段原始值
    :return: 净化后的可见 HTML 文本
    """
    return _sanitize_promotion_part(str(value or ""))


def _extract_promotion_map(result: Any, indexername: Optional[str]) -> dict[str, str]:
    """
    校验促销接口 JSON 载荷并提取每条种子的可见优惠标识映射。

    :param result: 接口响应解码后的 JSON 数据
    :param indexername: 站点名称，用于告警日志标识
    :return: 种子 ID 到净化后优惠 HTML 片段的映射；结构异常或全部不可见时返回空字典
    """
    if not isinstance(result, dict):
        logger.warn(f"{indexername} 促销状态响应结构异常，跳过促销状态补全")
        return {}
    if result.get("status") != 200:
        logger.warn(f"{indexername} 促销状态接口返回异常：{result.get('message')}")
        return {}
    states = result.get("message") or {}
    if not isinstance(states, dict):
        logger.warn(f"{indexername} 促销状态列表结构异常，跳过促销状态补全")
        return {}
    promotion_html: dict[str, str] = {}
    for torrent_id, state in states.items():
        state = state or {}
        if not isinstance(state, dict):
            logger.warn(f"{indexername} 种子 {torrent_id} 的促销状态结构异常，跳过促销状态补全")
            return {}
        fragment = _format_promotion_fragment(
            str(state.get("sp_state") or ""),
            str(state.get("timeout") or ""),
        )
        if fragment:
            promotion_html[str(torrent_id)] = fragment
    return promotion_html


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

        页面没有占位符或促销查询受 Turnstile 门控时不发起额外请求；促销接口失败或仅返回
        隐藏蜜罐时按无促销的原始页面解析，只损失优惠识别能力，不影响种子列表本身的获取。

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
        批量请求促销状态接口，返回种子 ID 到可见促销标识 HTML 的映射。

        :param html_text: 已解码的站点响应正文
        :return: 种子 ID 到 `<p>` 包裹的可见促销标识与限时文本的映射；跳过或失败时为空字典
        """
        if _is_turnstile_gated(html_text):
            logger.warn(f"{self.indexername} 促销状态接口受 Turnstile 验证保护，跳过促销状态补全")
            return {}
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
        return _extract_promotion_map(result, self.indexername)
