"""
站点地址匹配、页面登录状态与用户资料提取的纯规则。
"""

import base64
import json
import re
from typing import Any, Optional
from urllib.parse import urljoin

from lxml import etree

from app.foundation.dom import DomUtils
from app.foundation.url import split_netloc

_SPECIAL_SITE_DOMAINS = (
    "u2.dmhy.org",
    "pt.ecust.pp.ua",
    "pt.gtkpw.xyz",
    "pt.gtk.pw",
)

# 猪猪实验室公开文档指定的独立 API，不使用网页会话接口。
PIGGO_API_URL = "https://api.piggo.me/v1/api"
PIGGO_TOKEN_REQUIRED = "请在猪猪网页端「实验室」生成长期存取令牌，并填入站点 API Key 或 Token"


def piggo_access_token(apikey: Optional[str], token: Optional[str]) -> str:
    """
    选择猪猪长期存取令牌，优先使用非空 API Key，兼容 Token 配置。

    :param apikey: API Key 输入框中的实验室存取令牌
    :param token: 未填写 API Key 时使用的实验室存取令牌
    :return: 去掉首尾空白的令牌；两者都未配置时为空字符串
    """
    return str(apikey or "").strip() or str(token or "").strip()


def piggo_response_data(status_code: int, payload: object, access_token: str = "") -> dict[str, Any]:
    """
    校验猪猪实验室的 HTTP 和业务状态，阻止外层成功码掩盖必读公告。

    :param status_code: 原始 HTTP 响应状态码，非 200 不视为读取成功
    :param payload: 已解析的 JSON 响应
    :param access_token: 本次凭据，仅用于从站点错误文本中移除可能的回显
    :return: 成功响应的 data 对象
    :raises ValueError: 鉴权、必读公告、限流、其他业务失败或响应结构无效
    """
    # WAF 可能返回非 JSON 的鉴权或限流页，仍需保留 HTTP 失败语义。
    payload = payload if isinstance(payload, dict) else {}
    data = payload.get("data")
    details = data if isinstance(data, dict) else {}
    code = str(payload.get("code"))
    inner_code = str(details.get("code", "0"))
    message = str(details.get("message") or payload.get("msg") or payload.get("message") or "").strip()
    if code == "notice_unread" or inner_code == "notice_unread":
        gate = details.get("notice_gate") or {}
        title = str(gate.get("title") or "") if isinstance(gate, dict) else ""
        message = f"有未读必读公告，请到猪猪网页端阅读后重试{f'：{title}' if title else ''}"
    elif status_code == 401 or code == "401" or inner_code == "401":
        message = "猪猪存取令牌已过期或无效，请重新生成长期存取令牌"
    elif status_code == 429 or code == "429" or inner_code == "429":
        message = f"站点限流：{message or 'HTTP 429，请稍后重试'}"
    elif not payload and status_code == 200:
        message = "猪猪 API 响应结构无效"
    elif status_code != 200 or code != "0" or inner_code != "0":
        failure_code = inner_code if inner_code != "0" else code
        message = f"猪猪 API 请求失败，HTTP {status_code}，业务码 {failure_code}{f'：{message}' if message else ''}"
    elif isinstance(data, dict):
        return data
    else:
        message = "猪猪 API 响应缺少有效 data"
    if access_token:
        message = message.replace(access_token, "[已隐藏]")
    raise ValueError(" ".join(message.split())[:300])


def piggo_integer(value: object, minimum: int = 0) -> int:
    """
    校验实验室的整数 ID、计数和字节字段，不截断浮点数或接受布尔值。

    :param value: API 返回的整数或十进制整数字符串
    :param minimum: 字段下限，ID 和页码使用 1，计数及字节使用 0
    :return: 已校验的整数
    :raises ValueError: 字段缺失、格式错误或小于下限；错误不回显原值
    """
    if not isinstance(value, (str, int)) or isinstance(value, bool) or not re.fullmatch(r"0|[1-9][0-9]*", str(value)):
        raise ValueError("猪猪 API 整数字段无效")
    number = int(value)
    if number < minimum:
        raise ValueError("猪猪 API 整数字段无效")
    return number


def piggo_download_enclosure(api_url: str, torrent_id: object, access_token: str, proxy: bool = False,
                             ua: Optional[str] = None) -> str:
    """
    构造猪猪取种子时才签发 URL 的两段式配方，不预先消耗下载次数。

    :param api_url: 实验室 API 基础地址
    :param torrent_id: 正整数站内种子 ID
    :param access_token: 当前长期存取令牌，缺失时不生成配方
    :param proxy: 换票是否使用系统代理
    :param ua: 换票请求的浏览器 User-Agent，避免被站点前置 WAF 当作默认 Python 客户端
    :return: Base64 请求配置和 download-link 地址；ID 或凭据缺失时为空字符串
    """
    if not access_token or not re.fullmatch(r"[1-9][0-9]*", str(torrent_id or "")):
        return ""
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    if ua:
        headers["User-Agent"] = ua
    config = {
        "method": "post",
        "cookie": False,
        "header": headers,
        "json": {},
        "allow_redirects": False,
        "proxy": proxy,
        "protocol": "Piggo",
        "result": "data.url",
    }
    encoded = base64.b64encode(json.dumps(config).encode("utf-8")).decode("ascii")
    return f"[{encoded}]{api_url.rstrip('/')}/torrents/{torrent_id}/download-link"


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
    按 MoviePilot 站点规则提取用于匹配的注册域名。

    :param url: 待解析的站点地址；None 或空字符串返回空域名
    :return: 注册域名；无法解析时返回空字符串
    """
    if not url:
        return ""
    for domain in _SPECIAL_SITE_DOMAINS:
        if domain in url:
            return domain
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
