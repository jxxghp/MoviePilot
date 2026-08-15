from lxml import etree

from app.foundation.dom import DomUtils
from app.foundation.url import split_netloc


_SPECIAL_SITE_DOMAINS = (
    "u2.dmhy.org",
    "pt.ecust.pp.ua",
    "pt.gtkpw.xyz",
    "pt.gtk.pw",
)


def urls_match(first: str, second: str) -> bool:
    """判断两个地址是否指向忽略 www 前缀后的同一站点。"""
    if not first or not second:
        return False
    if first.startswith("http"):
        _scheme, first = split_netloc(first)
    if second.startswith("http"):
        _scheme, second = split_netloc(second)
    return first.replace("www.", "") == second.replace("www.", "")


def extract_domain(url: str) -> str:
    """按 MoviePilot 站点规则提取用于匹配的注册域名。"""
    if not url:
        return ""
    for domain in _SPECIAL_SITE_DOMAINS:
        if domain in url:
            return domain
    _scheme, netloc = split_netloc(url)
    if not netloc:
        return ""
    labels = netloc.split(".")
    if len(labels) > 3:
        return netloc
    return ".".join(labels[-2:])


class SiteUtils:
    """提供站点域名、Cookie 和访问参数处理能力。"""


    @classmethod
    def is_logged_in(cls, html_text: str) -> bool:
        """
        判断站点是否已经登陆
        :param html_text:
        :return:
        """
        html = etree.HTML(html_text)
        try:
            if not DomUtils.has_child_elements(html):
                return False
            # 存在明显的密码输入框，说明未登录
            if html.xpath("//input[@type='password']"):
                return False
            # 是否存在登出和用户面板等链接
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
        判断站点是否已经签到
        :return True已签到 False未签到
        """
        html = etree.HTML(html_text)
        try:
            if not DomUtils.has_child_elements(html):
                return False
            # 站点签到支持的识别XPATH
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
