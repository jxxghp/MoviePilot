import io
from pathlib import Path
from typing import Callable, Optional, List

from PIL import Image

from app.runtime.cache import cached, FileCache, AsyncFileCache
from app.application.configuration import get_chain_runtime_config_snapshot
from app.runtime.log import logger
from app.adapters.network.http import RequestUtils, AsyncRequestUtils
from app.adapters.network.ip import IpUtils
from app.application.security.url import SecurityUtils
from app.foundation.singleton import Singleton


WallpaperProvider = Callable[[], Optional[str]]
WallpaperListProvider = Callable[[int], List[str]]


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
        elif wallpaper == "tmdb":
            return self.get_tmdb_wallpapers(num)
        return []

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
        resp = RequestUtils(timeout=5).get_res(url)
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
        resp = RequestUtils(timeout=5).get_res(url)
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
            resp = RequestUtils(timeout=15).get_res(config.customize_wallpaper_api_url)
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
            proxies = config.proxy if not (referer or IpUtils.is_internal(url)) else None
        else:
            proxies = config.proxy if proxy else None
        return {
            "ua": config.normal_user_agent,
            "proxies": proxies,
            "referer": referer,
            "cookies": cookies,
            "accept_type": "image/avif,image/webp,image/apng,*/*",
        }

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
        if not url:
            return None

        cache_path = self._prepare_cache_path(url)

        # 检查缓存
        if use_cache:
            content = self.file_cache.get(cache_path, region="images")
            if content:
                mime_type = self.get_image_mime_type(content, verify=False)
                if mime_type:
                    return content, mime_type

        # 请求远程图片
        params = self._get_request_params(url, proxy, cookies)
        response = RequestUtils(**params).get_res(url=url)
        if response is None or response.status_code != 200:
            logger.warn(f"Failed to fetch image from URL: {url}")
            return None

        content = response.content
        mime_type = self.get_image_mime_type(content)
        if not mime_type:
            return None

        # 保存缓存
        self.file_cache.set(cache_path, content, region="images")
        return content, mime_type

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
        if not url:
            return None

        cache_path = self._prepare_cache_path(url)

        # 检查缓存
        if use_cache:
            content = await self.async_file_cache.get(cache_path, region="images")
            if content:
                mime_type = self.get_image_mime_type(content, verify=False)
                if mime_type:
                    return content, mime_type

        # 请求远程图片
        params = self._get_request_params(url, proxy, cookies)
        response = await AsyncRequestUtils(**params).get_res(url=url)
        if response is None or response.status_code != 200:
            logger.warn(f"Failed to fetch image from URL: {url}")
            return None

        content = response.content
        mime_type = self.get_image_mime_type(content)
        if not mime_type:
            return None

        # 保存缓存
        await self.async_file_cache.set(cache_path, content, region="images")
        return content, mime_type
