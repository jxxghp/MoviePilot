"""
站点地址匹配、页面登录状态与用户资料提取的纯规则。
"""

from io import BytesIO
from typing import Any, Callable, Mapping, Optional
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from lxml import etree

from app.foundation.dom import DomUtils
from app.foundation.url import UrlUtils, split_netloc

# 生成页返回的认证参数必须整体更新，不能混用旧凭据、签名或有效期。
_RSS_AUTH_PARAMS = frozenset({
    "passkey", "rsskey", "authkey", "key", "token", "auth", "auth_token",
    "access_token", "apikey", "api_key", "sign", "signature", "par", "uid", "userid", "user_id",
})
# 有效期和时间参数可能也是用户配置；只有生成页返回新值时才替换。
_RSS_TIME_PARAMS = frozenset({"timestamp", "expires", "expire", "expiry", "exp", "t"})


def normalize_rss_link(value: object, base_url: str, *, broad: bool = False) -> Optional[str]:
    """
    将生成页里的 RSS 地址补全为 HTTP(S) 地址，拒绝空锚点和页面脚本。

    :param value: XPath 提取的链接文本
    :param base_url: 生成页面地址，用于解析相对链接
    :param broad: 选择器匹配全部链接时，额外要求路径或参数带订阅标记
    :return: 可供抓取的完整地址；没有可信候选时为 None
    """
    if not isinstance(value, str) or not value.strip() or value.strip().startswith("#"):
        return None
    try:
        candidate = UrlUtils.normalize_http_url(urljoin(base_url, value.strip()))
    except ValueError:
        return None
    if candidate is None:
        return None
    parts = urlsplit(candidate)
    if parts.username is not None or parts.password is not None:
        return None
    if broad and not (
        any(marker in parts.path.lower() for marker in ("rss", "feed"))
        or any(key.lower() in _RSS_AUTH_PARAMS for key, _value in parse_qsl(parts.query))
    ):
        return None
    return candidate


def renew_rss_link(previous: Optional[str], generated: str) -> str:
    """
    用生成页的新认证参数续期，保留原链接的分类、条数等自定义筛选。

    使用生成页返回的订阅入口，兼容域名、协议和路径迁移。编码在路径或 par 中的
    不透明凭据随生成结果整体更新，最终可用性由调用方抓取验证。

    :param previous: 旧订阅地址；缺失或无效时直接使用生成结果
    :param generated: 已规范化的生成地址
    :return: 保留筛选并更新认证参数的候选地址
    """
    old_url = UrlUtils.normalize_http_url(previous)
    if old_url is None:
        return generated
    old, new = urlsplit(old_url), urlsplit(generated)
    generated_params = parse_qsl(new.query, keep_blank_values=True)
    refreshed_keys = _RSS_AUTH_PARAMS | {key.lower() for key, _value in generated_params
                                         if key.lower() in _RSS_TIME_PARAMS}
    old_filters = [(key, value) for key, value in parse_qsl(old.query, keep_blank_values=True)
                   if key.lower() not in refreshed_keys]
    filter_names = {key for key, _value in old_filters}
    new_params = [(key, value) for key, value in generated_params if key not in filter_names]
    # 路径本身携带票据时必须采用新路径，避免续期后继续访问旧票据。
    return urlunsplit((new.scheme, new.netloc, new.path, urlencode(old_filters + new_params), new.fragment))


def is_rss_document(content: str) -> bool:
    """
    只读取 XML 的首个元素，区分合法空订阅与登录页、错误页。

    :param content: 解码后的订阅响应，调用方已经限制总大小
    :return: 根元素是 RSS、Atom 或 RDF 时为 True
    """
    try:
        with BytesIO(content.encode("utf-8")) as stream:
            parser = etree.iterparse(stream, events=("start",), recover=True, resolve_entities=False,
                                     no_network=True, huge_tree=False)
            try:
                _event, root = next(parser)
                return etree.QName(root).localname.lower() in {"rss", "feed", "rdf"}
            finally:
                del parser
    except (ValueError, StopIteration, etree.XMLSyntaxError):
        return False


def urls_match(first: str, second: str) -> bool:
    """
    判断两个地址是否指向忽略 www 前缀后的同一站点。
    """
    if not first or not second:
        return False
    if first.startswith("http"):
        _scheme, first = split_netloc(first)
    if second.startswith("http"):
        _scheme, second = split_netloc(second)
    return first.replace("www.", "") == second.replace("www.", "")


def extract_domain(url: Optional[str]) -> str:
    """
    按通用层级规则提取域名，不解释具体站点的多级域名例外。

    :param url: 待解析的站点地址；None 或空字符串返回空域名
    :return: 注册域名；无法解析时返回空字符串
    """
    if not url:
        return ""
    _scheme, netloc = split_netloc(url)
    if not netloc:
        return ""
    # Cookie Domain 常带前导点；不去掉会被当成多一级域名，无法并入站点域名。
    netloc = netloc.lstrip(".")
    if not netloc:
        return ""
    labels = netloc.split(".")
    if len(labels) > 3:
        return netloc
    return ".".join(labels[-2:])


def resolve_page_url(site_url: str, page_path: Optional[str]) -> str:
    """
    将站点资源声明的页面路径解析为绝对地址。
    """
    page_path = str(page_path or "").strip()
    if not page_path:
        return site_url
    if page_path.startswith(("http://", "https://")):
        return page_path
    return urljoin(f"{str(site_url).rstrip('/')}/", page_path.lstrip("/"))


def is_valid_http_header_value(value: str) -> bool:
    """
    校验字符串能否作为合法的 HTTP 请求头值发送。

    拒绝首尾空白、控制字符（保留水平制表符）以及无法按 Latin-1 编码的字符，
    避免底层 HTTP 客户端在组装请求头阶段直接抛出异常。

    :param value: 待校验的请求头文本；空字符串视为合法
    :return: 可安全写入 HTTP 请求头时返回 True，否则返回 False
    """
    if not value:
        return True
    if value != value.strip():
        return False
    if any((ord(ch) < 0x20 and ch != "\t") or ord(ch) == 0x7F for ch in value):
        return False
    try:
        value.encode("latin-1")
    except UnicodeEncodeError:
        return False
    return True


def resolve_site_user_agent(
    site_ua: Optional[str],
    configured_ua: Optional[str],
    default_ua: str,
    *,
    host_default_ua: Optional[str] = None,
) -> str:
    """
    按“站点配置 > 通用配置 > 默认值”解析站点访问使用的 User-Agent。

    当单个站点未配置自定义值，或传入值等于宿主全局默认标识（兼容站点资源合并自动回填的默认标识）时，
    优先回退到系统设定中的通用站点 User-Agent；通用配置也为空时再使用兜底默认值。

    :param site_ua: 单个站点配置的 User-Agent，或经站点资源合并后的值
    :param configured_ua: 站点选项中配置的通用默认 User-Agent
    :param default_ua: 当前访问场景的兜底默认 User-Agent
    :param host_default_ua: 宿主全局默认 User-Agent；专站默认浏览器标识与宿主默认标识不同时用于识别资源回填值
    :return: 去除首尾空白后的最终请求 User-Agent
    """
    normalized_default = default_ua.strip() if isinstance(default_ua, str) else ""
    normalized_host_default = (
        host_default_ua.strip()
        if isinstance(host_default_ua, str)
        else normalized_default
    )
    normalized_site = site_ua.strip() if isinstance(site_ua, str) else ""
    if (
        normalized_site
        and normalized_site != normalized_default
        and normalized_site != normalized_host_default
    ):
        return normalized_site
    normalized_configured = (
        configured_ua.strip() if isinstance(configured_ua, str) else ""
    )
    if normalized_configured:
        return normalized_configured
    if normalized_site and normalized_site == normalized_default:
        return normalized_site
    return normalized_default


def read_optional_setting(
    setting_getter: Callable[[str], Any],
    key: str,
) -> Optional[str]:
    """
    通过注入的配置读取器安全读取可选字符串配置，兼容只声明部分键的测试桩。

    :param setting_getter: 外部注入的配置读取函数
    :param key: 配置项键名
    :return: 字符串配置值；缺失或为 None 时返回 None
    """
    try:
        value = setting_getter(key)
    except (AttributeError, KeyError):
        return None
    return str(value) if value is not None else None


def resolve_site_user_agent_from_settings(
    site_ua: Optional[str],
    setting_getter: Callable[[str], Any],
) -> str:
    """
    通过注入的设置读取函数按“站点配置 > 通用配置 > 默认值”解析站点 User-Agent。

    :param site_ua: 站点自身配置或资源合并后的 User-Agent
    :param setting_getter: 外部注入的运行时设置读取函数
    :return: 去除首尾空白后的最终请求 User-Agent
    """
    return resolve_site_user_agent(
        site_ua,
        read_optional_setting(setting_getter, "SITE_USER_AGENT"),
        read_optional_setting(setting_getter, "USER_AGENT") or "",
    )


def find_invalid_http_header_names(headers: Optional[Mapping[str, object]]) -> list[str]:
    """
    返回请求头映射中取值非法的字段名称，不暴露可能包含凭据的字段值。

    :param headers: 待检查的请求头映射；None 或空映射返回空列表
    :return: 取值不符合 HTTP 请求头规范的字段名称列表
    """
    if not headers:
        return []
    invalid_names: list[str] = []
    for key, value in headers.items():
        if value is None:
            continue
        if not isinstance(value, str) or not is_valid_http_header_value(value):
            invalid_names.append(str(key))
    return invalid_names


class SiteUtils:
    """
    提供站点域名、Cookie 和访问参数处理能力。
    """

    @classmethod
    def is_logged_in(cls, html_text: str) -> bool:
        """
        根据密码输入框和用户入口判断页面是否已登录。

        :param html_text: 待判断的站点 HTML 页面内容
        :return: 页面没有密码框且含登出或用户面板标记时返回 True，否则返回 False
        """
        html = etree.HTML(html_text)
        try:
            if not DomUtils.has_child_elements(html):
                return False
            # 存在明显的密码输入框，说明未登录
            if html.xpath("//input[@type='password']"):
                return False
            xpaths = [
                '//a[contains(@href, "logout")'
                ' or contains(@data-url, "logout")'
                ' or contains(@href, "mybonus") '
                ' or contains(@onclick, "logout")'
                ' or contains(@href, "usercp")'
                ' or contains(@lay-on, "logout")]',
                '//form[contains(@action, "logout")]',
                '//div[@class="user-info-side"]',
                '//a[@id="myitem"]'
            ]
            for xpath in xpaths:
                if html.xpath(xpath):
                    return True
            return False
        finally:
            if html is not None:
                del html

    @classmethod
    def is_checkin(cls, html_text: str) -> bool:
        """
        判断站点是否已经签到。

        :return True已签到 False未签到
        """
        html = etree.HTML(html_text)
        try:
            if not DomUtils.has_child_elements(html):
                return False
            xpaths = [
                '//a[@id="signed"]',
                '//a[contains(@href, "attendance")]',
                '//a[contains(text(), "签到")]',
                '//a/b[contains(text(), "签 到")]',
                '//span[@id="sign_in"]/a',
                '//a[contains(@href, "addbonus")]',
                '//input[@class="dt_button"][contains(@value, "打卡")]',
                '//a[contains(@href, "sign_in")]',
                '//a[contains(@onclick, "do_signin")]',
                '//a[@id="do-attendance"]',
                '//shark-icon-button[@href="attendance.php"]'
            ]
            for xpath in xpaths:
                if html.xpath(xpath):
                    return False
            return True
        finally:
            if html is not None:
                del html
