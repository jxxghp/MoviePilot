"""
图片安全代理、缓存、重定向与登录壁纸选择。
"""

import io
import threading
from collections.abc import Awaitable, Mapping, Sequence
from dataclasses import dataclass, field
from email.message import Message
from hashlib import sha256
from http.cookiejar import CookieJar
from http.cookies import CookieError, SimpleCookie
from pathlib import Path
from typing import Any, Callable, List, Optional, Protocol, TypeVar, cast
from urllib.parse import urljoin, urlsplit

from PIL import Image

from app.application.configuration import get_chain_runtime_config_snapshot
from app.application.security.url import SecurityUtils
from app.foundation.singleton import Singleton
from app.runtime.cache import AsyncFileCache, FileCache, cached
from app.runtime.log import logger

_WallpaperFunction = TypeVar("_WallpaperFunction", bound=Callable[..., object])


def _cached_wallpaper(*, skip_empty: bool = False) -> Callable[[_WallpaperFunction], _WallpaperFunction]:
    """
    保留壁纸函数签名，按原规则使用单项进程内缓存。
    """

    def decorate(function: _WallpaperFunction) -> _WallpaperFunction:
        """
        以相同有效期缓存壁纸，空列表仍按调用方策略处理。
        """
        return cached(
            maxsize=1, ttl=3600, skip_empty=skip_empty, local_only=True,
        )(function)

    return decorate


WallpaperProvider = Callable[[], Optional[str]]
WallpaperListProvider = Callable[[int], List[str]]
_IMAGE_CACHE_PATH_PREFIX = "proxy_"
_IMAGE_CACHE_HASH_LENGTH = 16
# Cover Art Archive 重定向后可能落到暂时异常的归档节点，短暂失败时重新请求原始地址
# 可选择其他节点。
_IMAGE_FETCH_ATTEMPTS = 2
_IMAGE_RETRY_STATUS_CODES = frozenset({408, 500, 502, 503, 504})
_IMAGE_REDIRECT_STATUS_CODES = frozenset({301, 302, 303, 307, 308})
_IMAGE_MAX_REDIRECTS = 5


@dataclass(frozen=True, slots=True)
class _ImageFetchRequest:
    """
    冻结同步与异步图片获取共用的缓存键和传输参数。

    :param url: 已解析并用于远端获取的图片 URL
    :param cache_path: 当前 URL 对应的图片缓存文件路径
    :param use_cache: 本次请求是否允许读取和写入图片缓存
    """

    url: str
    cache_path: str
    use_cache: bool


@dataclass
class _ImageCookieRequest:
    """
    为标准 CookieJar 提供纯请求元数据，不拥有网络传输能力。

    :param url: 当前跳转步骤的完整请求 URL，供 Cookie 域与路径匹配
    :param headers: 按小写名称保存 CookieJar 生成的请求头
    :param unverifiable: 是否属于无法验证来源的请求，默认作为独立可验证请求
    """

    url: str
    headers: dict[str, str] = field(default_factory=dict)
    unverifiable: bool = False

    @property
    def host(self) -> str:
        """
        返回 Cookie 端口匹配需要的网络位置。
        """
        return urlsplit(self.url).netloc

    @property
    def type(self) -> str:
        """
        返回 Secure Cookie 校验使用的 URL 协议。
        """
        return urlsplit(self.url).scheme

    @property
    def origin_req_host(self) -> str:
        """
        返回当前独立请求的来源主机。
        """
        return urlsplit(self.url).hostname or ""

    def get_full_url(self) -> str:
        """
        返回标准 Cookie 域和路径算法消费的完整 URL。
        """
        return self.url

    def get_header(self, name: str, default: Optional[str] = None) -> Optional[str]:
        """
        按大小写无关的名称读取 CookieJar 生成的请求头。
        """
        return self.headers.get(name.lower(), default)

    def has_header(self, name: str) -> bool:
        """
        判断 CookieJar 是否已经写入指定请求头。
        """
        return name.lower() in self.headers

    def add_unredirected_header(self, name: str, value: str) -> None:
        """
        记录 CookieJar 按域、路径及 Secure 规则筛选后的请求头。
        """
        self.headers[name.lower()] = value


@dataclass(frozen=True)
class _ImageCookieResponse:
    """
    只向标准 CookieJar 提供保留多个 Set-Cookie 的响应头。

    :param headers: 保留重复 Set-Cookie 字段的响应头对象
    """

    headers: Message

    def info(self) -> Message:
        """
        返回 CookieJar 规定的可读取重复头的元数据对象。
        """
        return self.headers


class ImageResponsePort(Protocol):
    """
    图片应用服务消费的最小 HTTP 响应契约。
    """

    @property
    def status_code(self) -> int:
        """
        返回响应状态码。

        :return: HTTP 响应状态码
        """
        ...

    @property
    def content(self) -> bytes:
        """
        返回完整响应内容。

        :return: 响应正文的完整字节内容
        """
        ...

    @property
    def headers(self) -> Mapping[str, str]:
        """
        返回通用响应头。

        :return: 用于缓存、重定向和内容类型判断的响应头映射
        """
        ...

    @property
    def set_cookie_headers(self) -> tuple[str, ...]:
        """
        返回保持独立的原始 Set-Cookie 响应头。

        :return: 按原始字段保留的 Set-Cookie 值，避免逗号合并破坏过期时间
        """
        ...

    def json(self) -> Any:
        """
        返回响应 JSON 载荷。

        :return: 响应正文解码后的 JSON 值
        """
        ...


class ImageTransport(Protocol):
    """
    图片与壁纸读取所需的同步、异步 GET 端口。
    """

    def get(
        self,
        url: str,
        *,
        options: Mapping[str, Any],
    ) -> Optional[ImageResponsePort]:
        """
        同步读取远端图片或壁纸响应。

        :param url: 当前请求的图片或壁纸接口 URL
        :param options: 请求头、超时、代理及重定向控制等客户端选项
        :return: 远端 HTTP 响应；未取得响应时为 None
        """
        ...

    async def async_get(
        self,
        url: str,
        *,
        options: Mapping[str, Any],
    ) -> Optional[ImageResponsePort]:
        """
        异步读取远端图片响应。

        :param url: 当前请求的图片或壁纸接口 URL
        :param options: 请求头、超时、代理及重定向控制等客户端选项
        :return: 远端 HTTP 响应；未取得响应时为 None
        """
        ...


class InternalAddressPort(Protocol):
    """
    判断 URL 是否指向无需代理的内部地址。
    """

    def is_internal(self, url: str) -> bool:
        """
        返回 URL 是否属于内部地址。

        :param url: 需要判定是否绕过代理的完整 URL
        :return: URL 指向内部地址时为 True
        """
        ...


_image_port_lock = threading.Lock()
_image_transport: Optional[ImageTransport] = None
_internal_address: Optional[InternalAddressPort] = None


def _clear_wallpaper_caches() -> None:
    """
    清除依赖外部 provider 或 transport 的壁纸函数缓存。
    """
    for method_name in (
        "get_tmdb_wallpaper",
        "get_tmdb_wallpapers",
        "get_bing_wallpaper",
        "get_bing_wallpapers",
        "get_mediaserver_wallpaper",
        "get_mediaserver_wallpapers",
        "get_customize_wallpaper",
        "get_customize_wallpapers",
    ):
        cache_clear = getattr(getattr(WallpaperHelper, method_name), "cache_clear", None)
        if callable(cache_clear):
            cache_clear()


def configure_image_ports(
    *,
    transport: ImageTransport,
    internal_address: InternalAddressPort,
) -> tuple[Optional[ImageTransport], Optional[InternalAddressPort]]:
    """
    由启动组合根装配图片 I/O 端口，并清除旧实现产生的壁纸缓存。
    """
    global _image_transport, _internal_address
    with _image_port_lock:
        previous = (_image_transport, _internal_address)
        _image_transport = transport
        _internal_address = internal_address
        _clear_wallpaper_caches()
    return previous


def reset_image_ports(
    transport: Optional[ImageTransport] = None,
    internal_address: Optional[InternalAddressPort] = None,
) -> None:
    """
    恢复指定图片端口；省略参数时回到未装配状态并清缓存。
    """
    global _image_transport, _internal_address
    with _image_port_lock:
        _image_transport = transport
        _internal_address = internal_address
        _clear_wallpaper_caches()


def _image_ports_snapshot() -> tuple[ImageTransport, InternalAddressPort]:
    """
    读取一致的图片端口快照，未装配时稳定失败。
    """
    with _image_port_lock:
        transport = _image_transport
        internal_address = _internal_address
    if transport is None or internal_address is None:
        raise RuntimeError("图片网络端口尚未由启动组合根装配")
    return transport, internal_address


def _redirect_private_ranges() -> tuple[str, ...]:
    """
    读取默认重定向校验使用的非公网放行网段。

    图片域名的首个请求由调用方（图片代理端点）带上
    ``IMAGE_PROXY_ALLOWED_PRIVATE_RANGES`` 完成校验，但内部调用方（推荐海报
    预缓存、通知配图）不传 ``redirect_validator``，重定向会退回本模块的默认
    校验。默认校验若固定不放行任何非公网网段，就会与首个请求的判定不一致：
    使用 fake-ip / 分流 DNS 时域名解析到保留地址，首个请求放行、跳转却被
    自己拦掉。此处沿用同一份配置，让两次校验口径一致。

    :return: 已配置的非公网 IP/CIDR 元组；运行时配置尚未装配时返回空元组，
        保持默认拒绝
    """
    try:
        snapshot = get_chain_runtime_config_snapshot()
    except RuntimeError:
        return ()
    return tuple(snapshot.image_proxy_allowed_private_ranges)


def _empty_wallpaper_provider() -> Optional[str]:
    """
    在启动组合根尚未装配壁纸来源时返回空结果。
    """
    return None


def _empty_wallpaper_list_provider(_count: int) -> List[str]:
    """
    在启动组合根尚未装配壁纸来源时返回空列表。
    """
    return []


_tmdb_wallpaper_provider: WallpaperProvider = _empty_wallpaper_provider
_tmdb_wallpaper_list_provider: WallpaperListProvider = (
    _empty_wallpaper_list_provider
)
_mediaserver_wallpaper_provider: WallpaperProvider = _empty_wallpaper_provider
_mediaserver_wallpaper_list_provider: WallpaperListProvider = (
    _empty_wallpaper_list_provider
)


def configure_wallpaper_providers(
    *,
    tmdb_wallpaper: WallpaperProvider,
    tmdb_wallpapers: WallpaperListProvider,
    mediaserver_wallpaper: WallpaperProvider,
    mediaserver_wallpapers: WallpaperListProvider,
) -> None:
    """
    由启动组合根注入需要业务 Chain 才能提供的壁纸来源。
    """
    global _tmdb_wallpaper_provider
    global _tmdb_wallpaper_list_provider
    global _mediaserver_wallpaper_provider
    global _mediaserver_wallpaper_list_provider
    _tmdb_wallpaper_provider = tmdb_wallpaper
    _tmdb_wallpaper_list_provider = tmdb_wallpapers
    _mediaserver_wallpaper_provider = mediaserver_wallpaper
    _mediaserver_wallpaper_list_provider = mediaserver_wallpapers
    _clear_wallpaper_caches()


def reset_wallpaper_providers() -> None:
    """
    恢复空壁纸来源并清除旧 lifespan 产生的壁纸缓存。
    """
    global _tmdb_wallpaper_provider
    global _tmdb_wallpaper_list_provider
    global _mediaserver_wallpaper_provider
    global _mediaserver_wallpaper_list_provider
    _tmdb_wallpaper_provider = _empty_wallpaper_provider
    _tmdb_wallpaper_list_provider = _empty_wallpaper_list_provider
    _mediaserver_wallpaper_provider = _empty_wallpaper_provider
    _mediaserver_wallpaper_list_provider = _empty_wallpaper_list_provider
    _clear_wallpaper_caches()


class WallpaperHelper(metaclass=Singleton):
    """
    壁纸帮助类

    壁纸缓存键不区分来源，启动装配与生命周期撤销时需要清空；每个方法只缓存一条地址，
    因此固定使用进程内缓存（local_only），清空不触及 Redis，也不跨进程沿用旧来源的结果。
    """

    def clear_cache(self) -> None:
        """
        清除全部壁纸来源缓存，使壁纸配置变更即时生效。
        """
        _clear_wallpaper_caches()

    def get_wallpaper(self) -> Optional[str]:
        """
        获取登录页面壁纸
        """
        wallpaper = get_chain_runtime_config_snapshot().wallpaper
        if wallpaper == "bing":
            return self.get_bing_wallpaper()
        elif wallpaper == "mediaserver":
            return self.get_mediaserver_wallpaper()
        elif wallpaper == "customize":
            return self.get_customize_wallpaper()
        elif wallpaper == "static":
            return self.get_static_wallpaper()
        elif wallpaper == "tmdb":
            return self.get_tmdb_wallpaper()
        return ''

    def get_wallpapers(self, num: int = 10) -> List[str]:
        """
        获取登录页面壁纸列表
        """
        wallpaper = get_chain_runtime_config_snapshot().wallpaper
        if wallpaper == "bing":
            return self.get_bing_wallpapers(num)
        elif wallpaper == "mediaserver":
            return self.get_mediaserver_wallpapers(num)
        elif wallpaper == "customize":
            return self.get_customize_wallpapers()
        elif wallpaper == "static":
            return self.get_static_wallpapers()
        elif wallpaper == "tmdb":
            return self.get_tmdb_wallpapers(num)
        return []

    def get_static_wallpaper(self) -> Optional[str]:
        """
        原样返回前端可访问的静态壁纸地址。
        """
        return get_chain_runtime_config_snapshot().wallpaper_image_url

    def get_static_wallpapers(self) -> List[str]:
        """
        以单项列表返回静态壁纸，确保前端沿用统一的列表契约。
        """
        wallpaper = self.get_static_wallpaper()
        return [wallpaper] if wallpaper else []

    @_cached_wallpaper()
    def get_tmdb_wallpaper(self) -> Optional[str]:
        """
        获取TMDB每日壁纸
        """
        return _tmdb_wallpaper_provider()

    @_cached_wallpaper(skip_empty=True)
    def get_tmdb_wallpapers(self, num: int = 10) -> List[str]:
        """
        获取7天的TMDB每日壁纸
        """
        return _tmdb_wallpaper_list_provider(num)

    @_cached_wallpaper()
    def get_bing_wallpaper(self) -> Optional[str]:
        """
        获取Bing每日壁纸
        """
        url = "https://cn.bing.com/HPImageArchive.aspx?format=js&idx=0&n=1"
        transport, _ = _image_ports_snapshot()
        resp = transport.get(url, options={"timeout": 5})
        if resp and resp.status_code == 200:
            try:
                result = resp.json()
                if isinstance(result, dict):
                    for image in result.get('images') or []:
                        return f"https://cn.bing.com{image.get('url')}" if 'url' in image else ''
            except Exception as err:
                print(str(err))
        return None

    @_cached_wallpaper(skip_empty=True)
    def get_bing_wallpapers(self, num: int = 7) -> List[str]:
        """
        获取7天的Bing每日壁纸
        """
        url = f"https://cn.bing.com/HPImageArchive.aspx?format=js&idx=0&n={num}"
        transport, _ = _image_ports_snapshot()
        resp = transport.get(url, options={"timeout": 5})
        if resp and resp.status_code == 200:
            try:
                result = resp.json()
                if isinstance(result, dict):
                    return [f"https://cn.bing.com{image.get('url')}" for image in result.get('images') or []]
            except Exception as err:
                print(str(err))
        return []

    @_cached_wallpaper()
    def get_mediaserver_wallpaper(self) -> Optional[str]:
        """
        获取媒体服务器壁纸
        """
        return _mediaserver_wallpaper_provider()

    @_cached_wallpaper(skip_empty=True)
    def get_mediaserver_wallpapers(self, num: int = 10) -> List[str]:
        """
        获取媒体服务器壁纸列表
        """
        return _mediaserver_wallpaper_list_provider(num)

    @_cached_wallpaper()
    def get_customize_wallpaper(self) -> Optional[str]:
        """
        获取自定义壁纸api壁纸
        """
        wallpaper_list = self.get_customize_wallpapers()
        if wallpaper_list:
            return wallpaper_list[0]
        return None

    @_cached_wallpaper(skip_empty=True)
    def get_customize_wallpapers(self) -> List[str]:
        """
        获取自定义壁纸api壁纸
        """

        def find_files_with_suffixes(obj: object, suffixes: Sequence[str]) -> List[str]:
            """
            递归查找对象中所有包含特定后缀的文件，返回匹配的字符串列表
            支持输入：字典、列表、字符串
            """
            _result = []

            if isinstance(obj, str):
                if obj.endswith(tuple(suffixes)):
                    _result.append(obj)

            elif isinstance(obj, dict):
                for value in obj.values():
                    _result.extend(find_files_with_suffixes(value, suffixes))

            elif isinstance(obj, list):
                for item in obj:
                    _result.extend(find_files_with_suffixes(item, suffixes))

            return _result

        config = get_chain_runtime_config_snapshot()
        if config.customize_wallpaper_api_url:
            wallpaper_list = []
            transport, _ = _image_ports_snapshot()
            resp = transport.get(
                config.customize_wallpaper_api_url,
                options={"timeout": 15},
            )
            if resp and resp.status_code == 200:
                content_type = resp.headers.get('Content-Type')
                if content_type and content_type.lower().startswith('image/'):
                    wallpaper_list.append(config.customize_wallpaper_api_url)
                else:
                    try:
                        result = resp.json()
                        if isinstance(result, list) or isinstance(result, dict) or isinstance(result, str):
                            wallpaper_list = find_files_with_suffixes(result, config.security_image_suffixes)
                    except Exception as err:
                        print(str(err))
            return wallpaper_list
        else:
            return []


class ImageHelper(metaclass=Singleton):
    """
    统一管理同步和异步图片缓存。
    """

    def __init__(self) -> None:
        """
        按全局图片缓存天数初始化文件缓存。

        图片缓存始终落在 ``CACHE_PATH/images``，即使启用了 Redis 缓存后端：图片是
        大体积二进制载荷，写入 Redis 会占据其绝大部分内存并放大 RDB 重写量。
        过期由启动清理任务按 ``GLOBAL_IMAGE_CACHE_DAYS`` 与文件修改时间回收；
        升级前已写入 Redis 的 ``images`` 键不再读取，由其自身 TTL 自然过期。
        """
        config = get_chain_runtime_config_snapshot()
        _base_path = config.cache_path
        _ttl = config.global_image_cache_days * 24 * 3600
        self.file_cache = FileCache(base=_base_path, ttl=_ttl, local_only=True)
        self.async_file_cache = AsyncFileCache(
            base=_base_path, ttl=_ttl, local_only=True
        )

    @staticmethod
    def _prepare_cache_path(url: str) -> str:
        """
        根据完整图片 URL 的哈希生成缓存路径，并保留原路径的扩展名。

        域名、端口和查询参数都参与缓存身份，避免同路径图片串图。
        旧的纯路径缓存无法确认来源，不回退读取，由既有过期清理任务回收。
        """
        cache_path = Path(SecurityUtils.sanitize_url_path(url))
        hash_value = sha256(url.encode()).hexdigest()[:_IMAGE_CACHE_HASH_LENGTH]
        return f"{_IMAGE_CACHE_PATH_PREFIX}{hash_value}{cache_path.suffix or '.jpg'}"

    @staticmethod
    def get_image_mime_type(content: bytes, verify: bool = True) -> Optional[str]:
        """
        根据图片内容返回 Pillow 识别的图片 MIME 类型。

        外部响应在写入缓存前需要完整校验；已校验的缓存只需读取格式头。
        非图片或可脚本化的 MIME 类型不作为图片代理响应。
        """
        if not content:
            return None
        try:
            with Image.open(io.BytesIO(content)) as image:
                image_format = (image.format or "").upper()
                if verify:
                    image.verify()
            mime_type: Optional[str] = Image.MIME.get(image_format)
            if (
                not mime_type
                or not mime_type.startswith("image/")
                or mime_type == "image/svg+xml"
            ):
                return None
            return mime_type
        except Exception as err:
            logger.warning(f"Invalid image format: {err}")
            return None

    @staticmethod
    def _get_request_params(url: str, proxy: Optional[bool], cookies: Optional[str | dict[str, str]]) -> dict[str, object]:
        """
        获取参数
        """
        referer = "https://movie.douban.com/" if "doubanio.com" in url else None
        config = get_chain_runtime_config_snapshot()
        if proxy is None:
            _, internal_address = _image_ports_snapshot()
            proxies = (
                config.proxy if not (referer or internal_address.is_internal(url)) else None
            )
        else:
            proxies = config.proxy if proxy else None
        return {
            "ua": config.normal_user_agent,
            "proxies": proxies,
            "referer": referer,
            "cookies": cookies,
            "accept_type": "image/avif,image/webp,image/apng,*/*",
        }

    @staticmethod
    def _fetch_request(
        url: str,
        proxy: Optional[bool],
        use_cache: bool,
        cookies: Optional[str | dict[str, str]],
    ) -> Optional[_ImageFetchRequest]:
        """
        统一空 URL 拒绝、缓存键和远端传输参数生成。
        """
        if not url:
            return None
        return _ImageFetchRequest(
            url=url,
            cache_path=ImageHelper._prepare_cache_path(url),
            use_cache=use_cache,
        )

    def _cached_image(
        self, content: Optional[bytes]
    ) -> Optional[tuple[bytes, str]]:
        """
        缓存内容只读取格式头，返回可直接复用的图片结果。
        """
        if not content:
            return None
        mime_type = self.get_image_mime_type(content, verify=False)
        return (content, mime_type) if mime_type else None

    def _fetched_image(
        self,
        request: _ImageFetchRequest,
        response: Optional[ImageResponsePort],
    ) -> Optional[tuple[bytes, str]]:
        """
        统一远端状态码与图片内容校验，拒绝结果不得进入缓存。
        """
        if response is None or response.status_code != 200:
            logger.warn(f"Failed to fetch image from URL: {request.url}")
            return None
        mime_type = self.get_image_mime_type(response.content)
        return (response.content, mime_type) if mime_type else None

    @staticmethod
    def _is_retryable_image_response(response: Optional[ImageResponsePort]) -> bool:
        """
        判断图片响应是否属于可立即重试的瞬态失败。
        """
        return response is None or response.status_code in _IMAGE_RETRY_STATUS_CODES

    @staticmethod
    def _redirect_target(response: ImageResponsePort, url: str) -> Optional[str]:
        """
        解析相对重定向，只接受不带 URL 凭据的 HTTP(S) 目标。
        """
        location = response.headers.get("location") or response.headers.get("Location")
        if not location:
            return None
        try:
            target = urljoin(url, location)
            parsed = urlsplit(target)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None:
                return None
            return target
        except ValueError:
            return None

    @staticmethod
    def _initial_redirect_cookies(url: str, options: Mapping[str, Any]) -> CookieJar:
        """
        为单次请求尝试建立独立 CookieJar，把调用者 Cookie 绑定到初始主机。
        """
        jar = CookieJar()
        cookies: SimpleCookie = SimpleCookie()
        try:
            value = options.get("cookies")
            if isinstance(value, str):
                cookies.load(value)
            elif isinstance(value, Mapping):
                for name, item in value.items():
                    cookies[name] = str(item)
        except CookieError:
            return jar
        headers = Message()
        for cookie in cookies.values():
            headers.add_header("Set-Cookie", f"{cookie.OutputString()}; Path=/")
        # 标准库类型桩限定具体 HTTP 类，实际 CookieJar 只消费这些纯元数据方法。
        jar.extract_cookies(cast(Any, _ImageCookieResponse(headers)), cast(Any, _ImageCookieRequest(url)))
        return jar

    @staticmethod
    def _redirect_options(
        options: Mapping[str, Any], source: str, target: str, response: ImageResponsePort, jar: CookieJar,
    ) -> dict[str, Any]:
        """
        同源沿用标准 Cookie 作用域；跨来源清除 Cookie/Referer，且不恢复早先凭据。
        """
        result = {**options, "allow_redirects": False}
        if urlsplit(source)[:2] != urlsplit(target)[:2]:
            jar.clear()
            result.update(cookies=None, cookie_header=None, referer=None)
            return result
        raw_headers = getattr(response, "set_cookie_headers", ())
        if not isinstance(raw_headers, (tuple, list)) or not raw_headers:
            value = response.headers.get("set-cookie") or response.headers.get("Set-Cookie")
            raw_headers = (value,) if isinstance(value, str) else ()
        headers = Message()
        for value in raw_headers:
            headers.add_header("Set-Cookie", value)
        jar.extract_cookies(cast(Any, _ImageCookieResponse(headers)), cast(Any, _ImageCookieRequest(source)))
        request = _ImageCookieRequest(target)
        jar.add_cookie_header(cast(Any, request))
        result.update(cookies=None, cookie_header=request.get_header("Cookie"))
        return result

    @staticmethod
    def _fetch_image_response(
        transport: ImageTransport,
        request: _ImageFetchRequest,
        options: Mapping[str, Any],
        redirect_validator: Optional[Callable[[str], bool]] = None,
    ) -> Optional[ImageResponsePort]:
        """
        逐跳校验后读取图片；重试从原地址开始，默认只允许公网与已配置网段。
        """
        response: Optional[ImageResponsePort] = None
        for _ in range(_IMAGE_FETCH_ATTEMPTS):
            current_url = request.url
            current_options = {**options, "allow_redirects": False}
            cookies = ImageHelper._initial_redirect_cookies(request.url, options)
            for redirect_count in range(_IMAGE_MAX_REDIRECTS + 1):
                response = transport.get(current_url, options=current_options)
                if response is None or response.status_code not in _IMAGE_REDIRECT_STATUS_CODES:
                    break
                target = ImageHelper._redirect_target(response, current_url)
                if not target or redirect_count == _IMAGE_MAX_REDIRECTS:
                    return response
                if redirect_validator:
                    allowed = redirect_validator(target)
                else:
                    allowed = SecurityUtils.is_safe_url(
                        target, {target}, strict=True, block_private=True,
                        allowed_private_ranges=_redirect_private_ranges(),
                    )
                if not allowed:
                    return response
                current_options = ImageHelper._redirect_options(current_options, current_url, target, response, cookies)
                current_url = target
            if not ImageHelper._is_retryable_image_response(response):
                break
        return response

    @staticmethod
    async def _async_fetch_image_response(
        transport: ImageTransport,
        request: _ImageFetchRequest,
        options: Mapping[str, Any],
        redirect_validator: Optional[Callable[[str], Awaitable[bool]]] = None,
    ) -> Optional[ImageResponsePort]:
        """
        异步逐跳校验后读取图片，拒绝目标不会被发起请求或进入缓存。
        """
        response: Optional[ImageResponsePort] = None
        for _ in range(_IMAGE_FETCH_ATTEMPTS):
            current_url = request.url
            current_options = {**options, "allow_redirects": False}
            cookies = ImageHelper._initial_redirect_cookies(request.url, options)
            for redirect_count in range(_IMAGE_MAX_REDIRECTS + 1):
                response = await transport.async_get(current_url, options=current_options)
                if response is None or response.status_code not in _IMAGE_REDIRECT_STATUS_CODES:
                    break
                target = ImageHelper._redirect_target(response, current_url)
                if not target or redirect_count == _IMAGE_MAX_REDIRECTS:
                    return response
                if redirect_validator:
                    allowed = await redirect_validator(target)
                else:
                    allowed = await SecurityUtils.is_safe_url_async(
                        target, {target}, strict=True, block_private=True,
                        allowed_private_ranges=_redirect_private_ranges(),
                    )
                if not allowed:
                    return response
                current_options = ImageHelper._redirect_options(current_options, current_url, target, response, cookies)
                current_url = target
            if not ImageHelper._is_retryable_image_response(response):
                break
        return response

    def fetch_image(
            self,
            url: str,
            proxy: Optional[bool] = None,
            use_cache: bool = True,
            cookies: Optional[str | dict[str, str]] = None) -> Optional[bytes]:
        """
        获取图片（同步版本）
        """
        result = self.fetch_image_with_mime_type(
            url=url,
            proxy=proxy,
            use_cache=use_cache,
            cookies=cookies,
        )
        return result[0] if result else None

    def fetch_image_with_mime_type(
        self,
        url: str,
        proxy: Optional[bool] = None,
        use_cache: bool = True,
        cookies: Optional[str | dict[str, str]] = None,
        redirect_validator: Optional[Callable[[str], bool]] = None,
    ) -> Optional[tuple[bytes, str]]:
        """
        同步获取图片及其内容识别 MIME 类型。

        网络响应在写入缓存前完整验证一次；缓存命中仅重新识别格式头。
        """
        request = ImageHelper._fetch_request(url, proxy, use_cache, cookies)
        if request is None:
            return None
        if request.use_cache:
            cached_result = self._cached_image(
                self.file_cache.get(request.cache_path, region="images")
            )
            if cached_result is not None:
                return cached_result
        transport, _ = _image_ports_snapshot()
        result = self._fetched_image(
            request,
            self._fetch_image_response(
                transport,
                request,
                ImageHelper._get_request_params(request.url, proxy, cookies),
                redirect_validator,
            ),
        )
        if result is None:
            return None
        self.file_cache.set(request.cache_path, result[0], region="images")
        return result

    async def async_fetch_image(
            self,
            url: str,
            proxy: Optional[bool] = None,
            use_cache: bool = True,
            cookies: Optional[str | dict[str, str]] = None) -> Optional[bytes]:
        """
        获取图片（异步版本）
        """
        result = await self.async_fetch_image_with_mime_type(
            url=url,
            proxy=proxy,
            use_cache=use_cache,
            cookies=cookies,
        )
        return result[0] if result else None

    async def async_fetch_image_with_mime_type(
        self,
        url: str,
        proxy: Optional[bool] = None,
        use_cache: bool = True,
        cookies: Optional[str | dict[str, str]] = None,
        redirect_validator: Optional[Callable[[str], Awaitable[bool]]] = None,
    ) -> Optional[tuple[bytes, str]]:
        """
        异步获取图片及其内容识别 MIME 类型。

        网络响应在写入缓存前完整验证一次；缓存命中仅重新识别格式头。
        """
        request = ImageHelper._fetch_request(url, proxy, use_cache, cookies)
        if request is None:
            return None
        if request.use_cache:
            cached_result = self._cached_image(
                await self.async_file_cache.get(request.cache_path, region="images")
            )
            if cached_result is not None:
                return cached_result
        transport, _ = _image_ports_snapshot()
        result = self._fetched_image(
            request,
            await self._async_fetch_image_response(
                transport,
                request,
                ImageHelper._get_request_params(request.url, proxy, cookies),
                redirect_validator,
            ),
        )
        if result is None:
            return None
        await self.async_file_cache.set(
            request.cache_path, result[0], region="images"
        )
        return result
