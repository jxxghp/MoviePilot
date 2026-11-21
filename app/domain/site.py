"""
站点地址匹配、页面登录状态与用户资料提取的纯规则。
"""

from io import BytesIO
from typing import Optional
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
