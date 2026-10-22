"""
站点访问技术端口的宿主组合根。
"""

from contextlib import contextmanager
from typing import Any, Callable, Iterator, Mapping, Optional, cast
from urllib.parse import urljoin, urlsplit

from app.adapters.external.cookiecloud import CookieCloudHelper
from app.adapters.external.ocr import OcrHelper
from app.adapters.network.browser import PlaywrightHelper
from app.adapters.network.cloudflare import under_challenge
from app.adapters.network.http import RequestUtils
from app.adapters.system import rust as rust_accel
from app.application.rss import (
    RssBrowserPort,
    RssHttpPort,
    RssParserPort,
    RssResponsePort,
    configure_rss_ports,
    reset_rss_ports,
)
from app.application.security.cookie import (
    CaptchaHttpPort,
    CaptchaOcrPort,
    CookieBrowserPort,
    CookieResult,
    configure_cookie_ports,
    reset_cookie_ports,
)
from app.application.torrent.download import (
    TorrentHttpPort,
    TorrentResponsePort,
    configure_torrent_port,
    reset_torrent_port,
)
from app.chain.site import (
    SiteBrowserPort,
    SiteChallengePort,
    SiteCookieCloudPort,
    SiteHttpPort,
    SiteResponsePort,
    configure_site_ports,
    reset_site_ports,
)
from app.foundation.url import UrlUtils


class _RssHttpAdapter:
    """
    用 RequestUtils 实现 RSS HTTP 窄端口。
    """

    def get(self, *, url: str, ua: Optional[str], headers: Optional[dict[str, Any]],
            proxies: Optional[dict[str, Any]], timeout: int) -> Optional[RssResponsePort]:
        """
        获取 RSS 原始响应。
        """
        return cast(
            Optional[RssResponsePort],
            RequestUtils(
                ua=ua or "",
                headers=headers or {},
                proxies=proxies or {},
                timeout=timeout,
            ).get_res(url),
        )

    def post(self, *, url: str, data: dict[str, Any], cookie: str, ua: str,
             proxies: Optional[dict[str, Any]], timeout: int) -> Optional[RssResponsePort]:
        """
        提交站点 RSS 链接表单。
        """
        return cast(
            Optional[RssResponsePort],
            RequestUtils(
                cookies=cookie,
                ua=ua,
                proxies=proxies or {},
                timeout=timeout,
            ).post_res(url=url, data=data),
        )

    def decode_xml(self, response: RssResponsePort, *, performance_mode: bool,
                   confidence_threshold: float) -> str:
        """
        复用宿主统一编码探测规则解码 RSS XML。
        """
        decoded: str = RequestUtils.get_decoded_xml_content(
            cast(Any, response),
            performance_mode=performance_mode,
            confidence_threshold=confidence_threshold,
        )
        return decoded


class _RssBrowserAdapter:
    """
    用 PlaywrightHelper 实现 RSS 页面渲染窄端口。
    """

    def render(self, *, url: str, cookie: str, ua: str,
               proxies: Optional[dict[str, Any]], timeout: int) -> Optional[str]:
        """
        渲染站点 RSS 配置页面并返回源码。
        """
        source: Optional[str] = PlaywrightHelper().get_page_source(
            url=url, cookies=cookie, ua=ua, proxies=proxies, timeout=timeout
        )
        return source


class _RssParserAdapter:
    """
    用可选 Rust 加速器实现 RSS 解析窄端口。
    """

    def parse(self, content: str, max_items: int) -> Optional[list[dict[str, Any]]]:
        """
        调用原生解析器，不可用时保留 None 兜底语义。
        """
        return rust_accel.parse_rss_items(content, max_items)


class _CookieBrowserAdapter:
    """
    用 PlaywrightHelper 实现站点登录浏览器窄端口。
    """

    def action(self, *, url: str, callback: Callable[[Any], CookieResult],
               proxies: Optional[dict[str, Any]], timeout: Optional[int]) -> CookieResult:
        """
        在浏览器受控会话中执行 Application 登录回调。
        """
        return cast(
            CookieResult,
            PlaywrightHelper().action(
                url=url, callback=callback, proxies=proxies, timeout=timeout
            ),
        )


class _CaptchaHttpAdapter:
    """
    用 RequestUtils 实现验证码图片下载窄端口。
    """

    def fetch(self, *, url: str, cookie: str, ua: str) -> Optional[bytes]:
        """
        下载验证码图片，仅向 Application 返回图片字节。
        """
        response = RequestUtils(ua=ua, cookies=cookie).get_res(url)
        return response.content if response and response.content else None


class _CaptchaOcrAdapter:
    """
    用 OcrHelper 实现验证码识别窄端口。
    """

    def recognize(self, image_data: bytes) -> str:
        """
        把原始验证码图片字节交给 OCR 服务识别。
        """
        text: str = OcrHelper().get_captcha_text(image_data=image_data)
        return text


class _TorrentHttpAdapter:
    """
    用 RequestUtils 实现种子下载 HTTP 窄端口。
    """

    def request(self, *, method: str, url: str, cookie: Optional[str],
                ua: Optional[str], referer: Optional[str], proxies: Optional[dict[str, Any]],
                allow_redirects: bool = True,
                data: Optional[dict[str, Any]] = None) -> Optional[TorrentResponsePort]:
        """
        按 Application 指定方法发起请求并保留响应三态。
        """
        request = RequestUtils(
            ua=ua or "",
            cookies=cookie or "",
            referer=referer or "",
            proxies=proxies or {},
        )
        if method == "POST":
            response = request.post_res(
                url=url, data=data, allow_redirects=allow_redirects
            )
        else:
            response = request.get_res(url=url, allow_redirects=allow_redirects)
        return cast(Optional[TorrentResponsePort], response)


class _SiteHttpAdapter:
    """
    用 RequestUtils 实现站点链有界 HTTP 响应端口。
    """

    @contextmanager
    def open(
        self,
        *,
        method: str,
        url: str,
        headers: Optional[Mapping[str, Any]] = None,
        cookie: Optional[str] = None,
        ua: Optional[str] = None,
        proxies: Optional[Mapping[str, Any]] = None,
        timeout: int = 20,
        json_body: Optional[Mapping[str, Any]] = None,
    ) -> Iterator[Optional[SiteResponsePort]]:
        """
        打开响应并在所有返回或异常分支后关闭底层连接。

        :param method: HTTP 请求方法
        :param url: 完整请求地址
        :param headers: 额外请求头
        :param cookie: 登录 Cookie
        :param ua: 用户代理字符串
        :param proxies: 代理配置
        :param timeout: 请求超时秒数
        :param json_body: JSON 请求体，None 时保留无请求体行为
        :yields: 在上下文有效期内提供响应或 None
        """
        request_headers = dict(headers or {})
        seen = set()
        for _hop in range(9):
            if url in seen:
                raise ValueError("站点请求重定向出现环路")
            seen.add(url)
            request = RequestUtils(
                headers=request_headers, cookies=cookie or "", ua=ua or "",
                proxies=dict(proxies or {}), timeout=timeout,
            )
            request_options: dict[str, Any] = {} if json_body is None else {"json": dict(json_body)}
            if method.upper() == "GET":
                request_options["allow_redirects"] = False
            with request.response_manager(method=method, url=url, **request_options) as response:
                if method.upper() != "GET" or response is None or response.status_code not in (301, 302, 303, 307, 308):
                    yield cast(Optional[SiteResponsePort], response)
                    return
                location = response.headers.get("Location")
                if not location:
                    raise ValueError("站点请求重定向缺少目标地址")
                destination = urljoin(url, location)
                target = urlsplit(destination)
                if target.scheme not in ("http", "https") or not target.hostname or target.username or target.password:
                    raise ValueError("站点请求重定向目标无效")
                if not UrlUtils.same_origin(url, destination):
                    cookie = None
                    request_headers = {
                        key: value for key, value in request_headers.items()
                        if key.lower() not in ("cookie", "authorization", "proxy-authorization", "referer")
                    }
                url = destination
        raise ValueError("站点请求重定向次数超过上限")


class _SiteBrowserAdapter:
    """
    用 PlaywrightHelper 实现站点链浏览器渲染端口。
    """

    def render(
        self,
        *,
        url: str,
        cookies: str,
        ua: str,
        proxies: Optional[Mapping[str, Any]],
        timeout: int,
    ) -> Optional[str]:
        """
        渲染页面；浏览器 helper 在返回前关闭页面与上下文。
        """
        source: Optional[str] = PlaywrightHelper().get_page_source(
            url=url,
            cookies=cookies,
            ua=ua,
            proxies=dict(proxies) if proxies else None,
            timeout=timeout,
        )
        return source


class _SiteChallengeAdapter:
    """
    用统一 Cloudflare 规则实现页面挑战识别端口。
    """

    def detected(self, html_text: str) -> bool:
        """
        返回页面是否处于站点挑战流程。
        """
        return bool(under_challenge(html_text))


class _SiteCookieCloudAdapter:
    """
    用 CookieCloudHelper 实现站点 Cookie 聚合端口。
    """

    def download(self) -> tuple[Optional[dict[str, str]], str]:
        """
        下载并返回 CookieCloud 的兼容二元结果。
        """
        return CookieCloudHelper().download()


def configure_site_access_composition() -> None:
    """
    统一装配 RSS、登录、种子下载与站点链技术边。
    """
    reset_site_access_composition()
    try:
        configure_rss_ports(
            http=cast(RssHttpPort, _RssHttpAdapter()),
            browser=cast(RssBrowserPort, _RssBrowserAdapter()),
            parser=cast(RssParserPort, _RssParserAdapter()),
        )
        configure_cookie_ports(
            browser=cast(CookieBrowserPort, _CookieBrowserAdapter()),
            http=cast(CaptchaHttpPort, _CaptchaHttpAdapter()),
            ocr=cast(CaptchaOcrPort, _CaptchaOcrAdapter()),
        )
        configure_torrent_port(cast(TorrentHttpPort, _TorrentHttpAdapter()))
        configure_site_ports(
            http=cast(SiteHttpPort, _SiteHttpAdapter()),
            browser=cast(SiteBrowserPort, _SiteBrowserAdapter()),
            challenge=cast(SiteChallengePort, _SiteChallengeAdapter()),
            cookiecloud=cast(SiteCookieCloudPort, _SiteCookieCloudAdapter()),
        )
    except Exception:
        reset_site_access_composition()
        raise


def reset_site_access_composition() -> None:
    """
    统一释放站点访问端口，支持重复 lifespan 与失败回滚。
    """
    reset_site_ports()
    reset_torrent_port()
    reset_cookie_ports()
    reset_rss_ports()
