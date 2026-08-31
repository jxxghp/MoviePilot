import io
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, List, Optional, Protocol

from PIL import Image

from app.application.configuration import get_chain_runtime_config_snapshot
from app.application.security.url import SecurityUtils
from app.foundation.singleton import Singleton
from app.runtime.cache import AsyncFileCache, FileCache, cached
from app.runtime.log import logger

WallpaperProvider = Callable[[], Optional[str]]
WallpaperListProvider = Callable[[int], List[str]]


@dataclass(frozen=True, slots=True)
class _ImageFetchRequest:
    """冻结同步与异步图片获取共用的缓存键和传输参数。"""

    url: str
    cache_path: str
    use_cache: bool


class ImageResponsePort(Protocol):
    """图片应用服务消费的最小 HTTP 响应契约。"""

    status_code: int
    content: bytes
    headers: Mapping[str, str]

    def json(self) -> Any:
        """返回响应 JSON 载荷。"""
        ...


class ImageTransport(Protocol):
    """图片与壁纸读取所需的同步、异步 GET 端口。"""

    def get(
        self,
        url: str,
        *,
        options: Mapping[str, Any],
    ) -> Optional[ImageResponsePort]:
        """同步读取远端图片或壁纸响应。"""
        ...

    async def async_get(
        self,
        url: str,
        *,
        options: Mapping[str, Any],
    ) -> Optional[ImageResponsePort]:
        """异步读取远端图片响应。"""
        ...


class InternalAddressPort(Protocol):
    """判断 URL 是否指向无需代理的内部地址。"""

    def is_internal(self, url: str) -> bool:
        """返回 URL 是否属于内部地址。"""
        ...


_image_port_lock = threading.Lock()
_image_transport: Optional[ImageTransport] = None
_internal_address: Optional[InternalAddressPort] = None


def _clear_wallpaper_caches() -> None:
    """清除依赖外部 provider 或 transport 的壁纸函数缓存。"""
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
    """由启动组合根装配图片 I/O 端口，并清除旧实现产生的壁纸缓存。"""
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
    """恢复指定图片端口；省略参数时回到未装配状态并清缓存。"""
    global _image_transport, _internal_address
    with _image_port_lock:
        _image_transport = transport
        _internal_address = internal_address
        _clear_wallpaper_caches()


def _image_ports_snapshot() -> tuple[ImageTransport, InternalAddressPort]:
    """读取一致的图片端口快照，未装配时稳定失败。"""
    with _image_port_lock:
        transport = _image_transport
        internal_address = _internal_address
    if transport is None or internal_address is None:
        raise RuntimeError("图片网络端口尚未由启动组合根装配")
    return transport, internal_address


def _empty_wallpaper_provider() -> Optional[str]:
    """在启动组合根尚未装配壁纸来源时返回空结果。"""
    return None


def _empty_wallpaper_list_provider(_count: int) -> List[str]:
    """在启动组合根尚未装配壁纸来源时返回空列表。"""
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
    """由启动组合根注入需要业务 Chain 才能提供的壁纸来源。"""
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
    """恢复空壁纸来源并清除旧 lifespan 产生的壁纸缓存。"""
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
    """

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
        """原样返回前端可访问的静态壁纸地址。"""
        return get_chain_runtime_config_snapshot().wallpaper_image_url

    def get_static_wallpapers(self) -> List[str]:
        """以单项列表返回静态壁纸，确保前端沿用统一的列表契约。"""
        wallpaper = self.get_static_wallpaper()
        return [wallpaper] if wallpaper else []

    @cached(maxsize=1, ttl=3600)
    def get_tmdb_wallpaper(self) -> Optional[str]:
        """
        获取TMDB每日壁纸
        """
        return _tmdb_wallpaper_provider()

    @cached(maxsize=1, ttl=3600, skip_empty=True)
    def get_tmdb_wallpapers(self, num: int = 10) -> List[str]:
        """
        获取7天的TMDB每日壁纸
        """
        return _tmdb_wallpaper_list_provider(num)

    @cached(maxsize=1, ttl=3600)
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

    @cached(maxsize=1, ttl=3600, skip_empty=True)
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

    @cached(maxsize=1, ttl=3600)
    def get_mediaserver_wallpaper(self) -> Optional[str]:
        """
        获取媒体服务器壁纸
        """
        return _mediaserver_wallpaper_provider()

    @cached(maxsize=1, ttl=3600, skip_empty=True)
    def get_mediaserver_wallpapers(self, num: int = 10) -> List[str]:
        """
        获取媒体服务器壁纸列表
        """
        return _mediaserver_wallpaper_list_provider(num)

    @cached(maxsize=1, ttl=3600)
    def get_customize_wallpaper(self) -> Optional[str]:
        """
        获取自定义壁纸api壁纸
        """
        wallpaper_list = self.get_customize_wallpapers()
        if wallpaper_list:
            return wallpaper_list[0]
        return None

    @cached(maxsize=1, ttl=3600, skip_empty=True)
    def get_customize_wallpapers(self) -> List[str]:
        """
        获取自定义壁纸api壁纸
        """

        def find_files_with_suffixes(obj, suffixes: List[str]) -> List[str]:
            """
            递归查找对象中所有包含特定后缀的文件，返回匹配的字符串列表
            支持输入：字典、列表、字符串
            """
            _result = []

            # 处理字符串
            if isinstance(obj, str):
                if obj.endswith(tuple(suffixes)):
                    _result.append(obj)

            # 处理字典
            elif isinstance(obj, dict):
                for value in obj.values():
                    _result.extend(find_files_with_suffixes(value, suffixes))

            # 处理列表
            elif isinstance(obj, list):
                for item in obj:
                    _result.extend(find_files_with_suffixes(item, suffixes))

            return _result

        # 判断是否存在自定义壁纸api
        config = get_chain_runtime_config_snapshot()
        if config.customize_wallpaper_api_url:
            wallpaper_list = []
            transport, _ = _image_ports_snapshot()
            resp = transport.get(
                config.customize_wallpaper_api_url,
                options={"timeout": 15},
            )
            if resp and resp.status_code == 200:
                # 如果返回的是图片格式
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
    """统一管理同步和异步图片缓存。"""

    def __init__(self):
        """按全局图片缓存天数初始化文件缓存。"""
        config = get_chain_runtime_config_snapshot()
        _base_path = config.cache_path
        _ttl = config.global_image_cache_days * 24 * 3600
        self.file_cache = FileCache(base=_base_path, ttl=_ttl)
        self.async_file_cache = AsyncFileCache(base=_base_path, ttl=_ttl)

    @staticmethod
    def _prepare_cache_path(url: str) -> str:
        """缓存路径"""
        sanitized_path = SecurityUtils.sanitize_url_path(url)
        cache_path = Path(sanitized_path)
        if not cache_path.suffix:
            cache_path = cache_path.with_suffix(".jpg")
        return cache_path.as_posix()

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
            mime_type = Image.MIME.get(image_format)
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
    def _get_request_params(url: str, proxy: Optional[bool], cookies: Optional[str | dict]) -> dict:
        """获取参数"""
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
        """统一空 URL 拒绝、缓存键和远端传输参数生成。"""
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
        """缓存内容只读取格式头，返回可直接复用的图片结果。"""
        if not content:
            return None
        mime_type = self.get_image_mime_type(content, verify=False)
        return (content, mime_type) if mime_type else None

    def _fetched_image(
        self,
        request: _ImageFetchRequest,
        response: Optional[ImageResponsePort],
    ) -> Optional[tuple[bytes, str]]:
        """统一远端状态码与图片内容校验，拒绝结果不得进入缓存。"""
        if response is None or response.status_code != 200:
            logger.warn(f"Failed to fetch image from URL: {request.url}")
            return None
        mime_type = self.get_image_mime_type(response.content)
        return (response.content, mime_type) if mime_type else None

    def fetch_image(
        self,
        url: str,
        proxy: Optional[bool] = None,
        use_cache: bool = True,
        cookies: Optional[str | dict] = None) -> Optional[bytes]:
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
        cookies: Optional[str | dict] = None,
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
            transport.get(
                request.url,
                options=ImageHelper._get_request_params(
                    request.url, proxy, cookies
                ),
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
        cookies: Optional[str | dict] = None) -> Optional[bytes]:
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
        cookies: Optional[str | dict] = None,
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
            await transport.async_get(
                request.url,
                options=ImageHelper._get_request_params(
                    request.url, proxy, cookies
                ),
            ),
        )
        if result is None:
            return None
        await self.async_file_cache.set(
            request.cache_path, result[0], region="images"
        )
        return result
