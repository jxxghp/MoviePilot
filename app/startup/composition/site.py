"""
站点访问技术端口的宿主组合根。
"""

from contextlib import ExitStack, contextmanager
from typing import Any, Callable, Iterator, Mapping, Optional, cast
from urllib.parse import urljoin, urlsplit

from app.adapters.external.cookiecloud import CookieCloudHelper
from app.adapters.external.ocr import OcrHelper
from app.adapters.network.browser import PlaywrightHelper
from app.adapters.network.cloudflare import under_challenge
from app.adapters.network.http import RequestUtils
from app.adapters.system import rust as rust_accel
from app.application.module import get_module_manager
from app.application.rss import (
    RssBrowserPort,
    RssHttpPort,
    RssLinkRecipe,
    RssParserPort,
    RssResponsePort,
    RssSitePort,
    configure_rss_ports,
    configure_rss_site_port,
    reset_rss_ports,
    reset_rss_site_port,
)
from app.application.security.cookie import (
    CaptchaHttpPort,
    CaptchaOcrPort,
    CookieBrowserPort,
    CookieResult,
    configure_cookie_ports,
    reset_cookie_ports,
)
from app.application.site.protocol import (
    SiteProtocolPort,
    configure_site_protocol_port,
    reset_site_protocol_port,
    site_domain,
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
from app.domain.context import TorrentInfo
from app.foundation.url import UrlUtils, url_origin_for_log
from app.runtime.extensions.module.dispatcher import ModuleInvocationDispatcher
from app.runtime.log import logger


class _SiteModuleCatalog:
    """
    按需读取当前模块目录；站点内部协议不访问插件目录或触发插件物化。
    """

    @staticmethod
    def get_running_modules(method: str) -> Any:
        """
        返回本次调用实现指定能力的活动宿主模块。

        :param method: 宿主内部的站点能力名称
        :return: 当前活动模块快照
        """
        return get_module_manager().get_running_modules(method)

    @staticmethod
    def get_plugin_modules() -> Mapping[tuple[str, str], Mapping[str, Callable[..., Any]]]:
        """
        提供不物化插件的空目录，站点内部能力不向插件开放。

        :return: 空插件目录
        """
        return {}


def _raise_site_module_error(error: Exception, *_args: Any, **_kwargs: Any) -> None:
    """
    保留站点协议失败给用例判断，不发布模块系统告警。

    :param error: 模块分发或协议实现抛出的失败
    :param _args: 调度器提供的调用位置，仅供兼容统一回调
    :param _kwargs: 调度器提供的原始调用参数，不输出凭据
    :raises Exception: 原始协议失败
    """
    raise error


class _SiteModuleAdapter:
    """
    将 Application 的站点和 RSS 窄端口接到同一宿主模块分发算法。
    """

    def __init__(self, dispatcher: ModuleInvocationDispatcher) -> None:
        """
        保存无资源所有权的调度器，构造时不发现站点或活动模块。

        :param dispatcher: 由组合根创建的宿主模块调度器
        """
        self._dispatcher = dispatcher

    def get_site_domain(self, url: Optional[str]) -> str:
        """
        分发站点域名规则，缺失模块不能被误报为通用站点。

        :param url: 站点地址或 Cookie Domain
        :return: 模块解释的站点匹配域名
        :raises RuntimeError: 模块没有提供域名结果
        """
        result = self._dispatcher.dispatch_strict("get_site_domain", url=url)
        if not isinstance(result, str):
            raise RuntimeError("站点协议 Module 不可用")
        return result

    def torrent_resource_identity(self, torrent: TorrentInfo) -> Optional[tuple[str, str]]:
        """
        分发资源身份解释，保留无法确定身份的空结果。

        :param torrent: 旧缓存或当前候选，不修改输入
        :return: 站点及稳定资源标识；无法确定时为 None
        """
        return cast(Optional[tuple[str, str]], self._dispatcher.dispatch_strict("torrent_resource_identity", torrent=torrent))

    def is_expiring_download_url(self, url: Optional[str]) -> bool:
        """
        分发短时票据识别。

        :param url: 原始或重定向后的下载地址
        :return: 是否属于已支持的短时票据
        :raises RuntimeError: 模块没有提供明确判断
        """
        result = self._dispatcher.dispatch_strict("is_expiring_download_url", url=url)
        if not isinstance(result, bool):
            raise RuntimeError("站点下载协议 Module 不可用")
        return result

    def is_torrent_download_confirmation(self, content: bytes) -> bool:
        """
        分发首次下载确认响应识别。

        :param content: 尚未成功解析为种子的原始响应
        :return: 是否需要按站点协议提交确认
        :raises RuntimeError: 模块没有提供明确判断
        """
        result = self._dispatcher.dispatch_strict("is_torrent_download_confirmation", content=content)
        if not isinstance(result, bool):
            raise RuntimeError("站点下载协议 Module 不可用")
        return result

    def parse_torrent_download_confirmation(self, text: str, url: str) -> Optional[tuple[str, dict[str, object]]]:
        """
        分发确认表单解释，只向调用方交还安全的地址和字段。

        :param text: 确认页面 HTML 文本
        :param url: 确认页的最终请求地址，作为提交的来源边界
        :return: 安全的地址及控件；没有合法表单时为 None
        """
        return cast(Optional[tuple[str, dict[str, object]]], self._dispatcher.dispatch_strict(
            "parse_torrent_download_confirmation", text=text, url=url,
        ))

    def has_site_subtitle_api(self, site: Mapping[str, object]) -> bool:
        """
        按模块声明判断独立字幕协议，不识别站点名称。

        :param site: 站点资源声明
        :return: 是否由独立接口提供字幕
        :raises RuntimeError: 模块没有提供明确判断
        """
        result = self._dispatcher.dispatch_strict("has_site_subtitle_api", site=dict(site))
        if not isinstance(result, bool):
            raise RuntimeError("站点字幕协议 Module 不可用")
        return result

    def get_site_rss_recipe(self, url: str) -> RssLinkRecipe:
        """
        分发 RSS 获取配方；模块缺失时明确失败，不使用旧站点表回退。

        :param url: 站点根地址
        :return: 站点或通用 RSS 获取配方
        :raises RuntimeError: 模块没有提供配方
        """
        result = self._dispatcher.dispatch_strict("get_site_rss_recipe", url=url)
        if not isinstance(result, RssLinkRecipe):
            raise RuntimeError("RSS 站点协议 Module 不可用")
        return result

    def is_site_rss_expired(self, text: str) -> bool:
        """
        分发 RSS 过期响应解释。

        :param text: RSS 响应正文
        :return: 是否需要重新获取 RSS 地址
        :raises RuntimeError: 模块没有提供明确判断
        """
        result = self._dispatcher.dispatch_strict("is_site_rss_expired", text=text)
        if not isinstance(result, bool):
            raise RuntimeError("RSS 站点协议 Module 不可用")
        return result

    def encrypt_site_payload(self, data_str: str, key: bytes) -> str:
        """
        分发旧插件所需的站点协议加密，不把协议包装放回通用安全模块。

        :param data_str: 待加密的站点文本
        :param key: 协议约定的密钥字节
        :return: 兼容旧接口的加密载荷
        :raises RuntimeError: 模块没有提供加密结果
        """
        result = self._dispatcher.dispatch_strict("encrypt_site_payload", data_str=data_str, key=key)
        if not isinstance(result, str):
            raise RuntimeError("站点加密协议 Module 不可用")
        return result


def configure_site_protocol_composition() -> None:
    """
    同时装配站点和 RSS 协议端口，不在此阶段物化 Module。
    """
    catalog = _SiteModuleCatalog()
    adapter = _SiteModuleAdapter(ModuleInvocationDispatcher(
        module_catalog=catalog, plugin_catalog=catalog,
        plugin_error_handler=_raise_site_module_error,
        system_error_handler=_raise_site_module_error,
        rate_limit_handler=_raise_site_module_error,
    ))
    configure_site_protocol_port(cast(SiteProtocolPort, adapter))
    configure_rss_site_port(cast(RssSitePort, adapter))


def reset_site_protocol_composition() -> None:
    """
    逆序撤销站点与 RSS 协议端口，关闭后不再调用模块目录。
    """
    reset_rss_site_port()
    reset_site_protocol_port()


def _redirect_destination(url: str, response: Any) -> tuple[Optional[str], Optional[str]]:
    """
    解析一跳重定向的目标地址，只接受不带 URL 凭据的 HTTP(S) 目标。

    :param url: 当前跳的请求地址
    :param response: 当前跳的重定向响应
    :return: 目标地址与失败原因，二者恰有一个非空
    """
    location = response.headers.get("Location")
    if not location:
        return None, "站点请求重定向缺少目标地址"
    try:
        destination = urljoin(url, location)
        target = urlsplit(destination)
    except ValueError:
        return None, "站点请求重定向目标无效"
    if target.scheme not in ("http", "https") or not target.hostname or target.username or target.password:
        return None, "站点请求重定向目标无效"
    return destination, None


def _redirect_keeps_credentials(source: str, destination: str) -> bool:
    """
    判断站点重定向是否仍可携带原请求的登录凭据。

    除同源跳转外，仅允许同主机从默认 HTTP 端口升级到默认 HTTPS 端口，
    避免站点强制 HTTPS 时丢失 Cookie；协议降级和其它来源变更均不保留凭据。

    :param source: 当前请求的绝对地址
    :param destination: 重定向目标的绝对地址
    :return: 两地址同源或符合默认端口的安全升级条件时返回 True
    """
    if UrlUtils.same_origin(source, destination):
        return True
    source_url = UrlUtils.normalize_http_url(source)
    destination_url = UrlUtils.normalize_http_url(destination)
    if source_url is None or destination_url is None:
        return False
    origin, target = urlsplit(source_url), urlsplit(destination_url)
    return (
        origin.scheme == "http"
        and target.scheme == "https"
        and origin.hostname == target.hostname
        and origin.port in (None, 80)
        and target.port in (None, 443)
        and not (origin.username or origin.password or target.username or target.password)
    )


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

        GET 请求逐跳跟随重定向，同源或同主机默认端口的 HTTP 到 HTTPS 升级保留凭据，
        其它来源变更清除 Cookie 与认证头。重定向缺少目标、目标无效、出现环路或超过跳数上限时
        只记录来源地址的告警并提供 None，与请求失败的语义一致，
        由调用方按无法打开站点处理，不能以异常中断 CookieCloud 同步等批量流程。

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
        failure = "站点请求重定向次数超过上限"
        for _hop in range(9):
            if url in seen:
                failure = "站点请求重定向出现环路"
                break
            seen.add(url)
            request = RequestUtils(
                headers=request_headers, cookies=cookie or "", ua=ua or "",
                proxies=dict(proxies or {}), timeout=timeout,
            )
            request_options: dict[str, Any] = {} if json_body is None else {"json": dict(json_body)}
            if method.upper() == "GET":
                request_options["allow_redirects"] = False
            with ExitStack() as stack:
                try:
                    response = stack.enter_context(
                        request.response_manager(method=method, url=url, **request_options)
                    )
                except ValueError:
                    # 请求地址或响应 Location 畸形时 requests 会在打开响应时抛出，禁用自动跟随也会预解析 Location
                    failure = "站点请求地址或重定向目标无效"
                    break
                if method.upper() != "GET" or response is None or response.status_code not in (301, 302, 303, 307, 308):
                    yield cast(Optional[SiteResponsePort], response)
                    return
                destination, invalid_reason = _redirect_destination(url, response)
                if destination is None:
                    failure = invalid_reason or failure
                    break
                if not _redirect_keeps_credentials(url, destination):
                    cookie = None
                    request_headers = {
                        key: value for key, value in request_headers.items()
                        if key.lower() not in ("cookie", "authorization", "proxy-authorization", "referer")
                    }
                url = destination
        logger.warning(f"{failure}：{url_origin_for_log(url)}")
        yield None


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
        return CookieCloudHelper(domain_resolver=site_domain).download()


def configure_site_access_composition() -> None:
    """
    统一装配 RSS、登录、种子下载与站点链技术边。
    """
    reset_site_access_composition()
    try:
        configure_site_protocol_composition()
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
    reset_site_protocol_composition()
