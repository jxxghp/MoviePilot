import io
import json
import hashlib
import threading
from pathlib import Path
from typing import Awaitable, Callable, Optional, List
from urllib.parse import urljoin, urlparse

from PIL import Image

from app.chain.mediaserver import MediaServerChain
from app.chain.tmdb import TmdbChain
from app.core.cache import cached, FileCache, AsyncFileCache
from app.core.config import settings
from app.log import logger
from app.utils.http import RequestUtils, AsyncRequestUtils
from app.utils.ip import IpUtils
from app.utils.security import SecurityUtils
from app.utils.singleton import Singleton


class WallpaperHelper(metaclass=Singleton):
    """
    壁纸帮助类
    """

    _CATALOG_LIMIT = 64
    _CATALOG_VERSION = 2

    def __init__(self):
        self._catalog_lock = threading.RLock()
        self._catalog_refreshing = False
        self._catalog_sources: dict[str, str] = {}
        self._catalog_active: list[str] = []
        self._catalog_order: list[str] = []
        self._catalog_path = settings.CACHE_PATH / "login_wallpapers" / "catalog.json"
        self._load_catalog()

    @staticmethod
    def _catalog_id(url: str) -> str:
        """生成不可逆且跨进程稳定的壁纸目录标识。"""
        return hashlib.sha256(url.encode("utf-8")).hexdigest()[:32]

    @staticmethod
    def _is_catalog_source(url: object) -> bool:
        """目录只接受后端壁纸来源返回的 HTTP(S) 图片地址。"""
        return isinstance(url, str) and url.startswith(("http://", "https://"))

    def _load_catalog(self) -> None:
        """从本地缓存恢复最后一次成功目录，使冷启动无需等待远端来源。"""
        try:
            payload = json.loads(self._catalog_path.read_text(encoding="utf-8"))
            version = payload.get("version")
            if version not in {1, self._CATALOG_VERSION}:
                return
            entries = payload.get("wallpapers") or []
            sources = {
                entry["id"]: entry["url"]
                for entry in entries
                if isinstance(entry, dict)
                and isinstance(entry.get("id"), str)
                and self._is_catalog_source(entry.get("url"))
                and entry["id"] == self._catalog_id(entry["url"])
            }
            order = list(
                dict.fromkeys(
                    entry["id"]
                    for entry in entries
                    if isinstance(entry, dict) and entry.get("id") in sources
                )
            )[: self._CATALOG_LIMIT]
            active = payload.get("active") if version == self._CATALOG_VERSION else order[:10]
            self._catalog_order = order
            self._catalog_sources = {item: sources[item] for item in order}
            self._catalog_active = list(
                dict.fromkeys(
                    item
                    for item in (active or [])
                    if isinstance(item, str) and item in self._catalog_sources
                )
            )[: self._CATALOG_LIMIT]
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            self._catalog_sources = {}
            self._catalog_active = []
            self._catalog_order = []

    def _persist_catalog(self) -> None:
        """原子写入有界目录，避免进程退出时留下半写入 JSON。"""
        self._catalog_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = self._catalog_path.with_suffix(".tmp")
        payload = {
            "version": self._CATALOG_VERSION,
            "active": self._catalog_active,
            "wallpapers": [
                {"id": wallpaper_id, "url": self._catalog_sources[wallpaper_id]}
                for wallpaper_id in self._catalog_order
                if wallpaper_id in self._catalog_sources
            ],
        }
        temporary_path.write_text(json.dumps(payload, ensure_ascii=True), encoding="utf-8")
        temporary_path.chmod(0o600)
        temporary_path.replace(self._catalog_path)

    def register_wallpaper_catalog(self, urls: List[str]) -> List[str]:
        """
        登记后端来源已选择的壁纸，并返回当前列表的 opaque ID。

        旧目录项在容量允许时继续保留，保证交叉淡化或旧页面的迟到图片请求仍可完成。
        """
        valid_urls = list(
            dict.fromkeys(url for url in urls if self._is_catalog_source(url))
        )[: self._CATALOG_LIMIT]
        if not valid_urls:
            return self.get_wallpaper_catalog_ids()

        with self._catalog_lock:
            current_ids = [self._catalog_id(url) for url in valid_urls]
            preserved_ids = [item for item in self._catalog_order if item not in current_ids]
            self._catalog_active = current_ids
            self._catalog_order = (current_ids + preserved_ids)[: self._CATALOG_LIMIT]
            for url in valid_urls:
                self._catalog_sources[self._catalog_id(url)] = url
            self._catalog_sources = {
                item: self._catalog_sources[item]
                for item in self._catalog_order
                if item in self._catalog_sources
            }
            try:
                self._persist_catalog()
            except OSError as err:
                logger.warning(f"登录壁纸目录写入失败: {err}")
            return current_ids

    def get_wallpaper_catalog_ids(self, limit: int = 10) -> List[str]:
        """读取最后一次成功目录的活动顺序，不触发远端来源请求。"""
        with self._catalog_lock:
            return self._catalog_active[:limit]

    def get_wallpaper_catalog_source(self, wallpaper_id: str) -> Optional[str]:
        """仅解析已登记 opaque ID，客户端无法借此指定任意抓取目标。"""
        with self._catalog_lock:
            return self._catalog_sources.get(wallpaper_id)

    def refresh_wallpaper_catalog(self, num: int = 10) -> List[str]:
        """刷新来源并保留失败前的最后成功目录，同一进程内只允许一个刷新任务。"""
        with self._catalog_lock:
            if self._catalog_refreshing:
                return self._catalog_active[:num]
            self._catalog_refreshing = True
        try:
            self.register_wallpaper_catalog(self.get_wallpapers(num))
            return self.get_wallpaper_catalog_ids(num)
        finally:
            with self._catalog_lock:
                self._catalog_refreshing = False

    def get_wallpaper(self) -> Optional[str]:
        """
        获取登录页面壁纸
        """
        if settings.WALLPAPER == "bing":
            return self.get_bing_wallpaper()
        elif settings.WALLPAPER == "mediaserver":
            return self.get_mediaserver_wallpaper()
        elif settings.WALLPAPER == "customize":
            return self.get_customize_wallpaper()
        elif settings.WALLPAPER == "tmdb":
            return self.get_tmdb_wallpaper()
        return ''

    def get_wallpapers(self, num: int = 10) -> List[str]:
        """
        获取登录页面壁纸列表
        """
        if settings.WALLPAPER == "bing":
            return self.get_bing_wallpapers(num)
        elif settings.WALLPAPER == "mediaserver":
            return self.get_mediaserver_wallpapers(num)
        elif settings.WALLPAPER == "customize":
            return self.get_customize_wallpapers()
        elif settings.WALLPAPER == "tmdb":
            return self.get_tmdb_wallpapers(num)
        return []

    @cached(maxsize=1, ttl=3600)
    def get_tmdb_wallpaper(self) -> Optional[str]:
        """
        获取TMDB每日壁纸
        """
        return TmdbChain().get_random_wallpager()

    @cached(maxsize=1, ttl=3600, skip_empty=True)
    def get_tmdb_wallpapers(self, num: int = 10) -> List[str]:
        """
        获取7天的TMDB每日壁纸
        """
        return TmdbChain().get_trending_wallpapers(num)

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
        return MediaServerChain().get_latest_wallpaper()

    @cached(maxsize=1, ttl=3600, skip_empty=True)
    def get_mediaserver_wallpapers(self, num: int = 10) -> List[str]:
        """
        获取媒体服务器壁纸列表
        """
        return MediaServerChain().get_latest_wallpapers(count=num)

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
        if settings.CUSTOMIZE_WALLPAPER_API_URL:
            wallpaper_list = []
            resp = RequestUtils(timeout=15).get_res(settings.CUSTOMIZE_WALLPAPER_API_URL)
            if resp and resp.status_code == 200:
                # 如果返回的是图片格式
                content_type = resp.headers.get('Content-Type')
                if content_type and content_type.lower().startswith('image/'):
                    wallpaper_list.append(settings.CUSTOMIZE_WALLPAPER_API_URL)
                else:
                    try:
                        result = resp.json()
                        if isinstance(result, list) or isinstance(result, dict) or isinstance(result, str):
                            wallpaper_list = find_files_with_suffixes(result, settings.SECURITY_IMAGE_SUFFIXES)
                    except Exception as err:
                        print(str(err))
            return wallpaper_list
        else:
            return []


class ImageHelper(metaclass=Singleton):

    def __init__(self):
        _base_path = settings.CACHE_PATH
        _ttl = settings.GLOBAL_IMAGE_CACHE_DAYS * 24 * 3600
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
    def _validate_image(content: bytes, max_pixels: Optional[int] = None) -> bool:
        """验证图片格式，并可限制解码后的像素总量。"""
        if not content:
            return False
        try:
            with Image.open(io.BytesIO(content)) as image:
                if max_pixels is not None and image.width * image.height > max_pixels:
                    logger.warning(
                        "图片像素超过允许上限: %sx%s > %s",
                        image.width,
                        image.height,
                        max_pixels,
                    )
                    return False
                image.verify()
            return True
        except Exception as e:
            logger.warn(f"Invalid image format: {e}")
            return False

    @staticmethod
    def _get_request_params(url: str, proxy: Optional[bool], cookies: Optional[str | dict]) -> dict:
        """获取参数"""
        referer = "https://movie.douban.com/" if "doubanio.com" in url else None
        if proxy is None:
            proxies = settings.PROXY if not (referer or IpUtils.is_internal(url)) else None
        else:
            proxies = settings.PROXY if proxy else None
        return {
            "ua": settings.NORMAL_USER_AGENT,
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
        if not url:
            return None

        cache_path = self._prepare_cache_path(url)

        # 检查缓存
        if use_cache:
            content = self.file_cache.get(cache_path, region="images")
            if content:
                return content

        # 请求远程图片
        params = self._get_request_params(url, proxy, cookies)
        response = RequestUtils(**params).get_res(url=url)
        if response is None or response.status_code != 200:
            logger.warn(f"Failed to fetch image from URL: {url}")
            return None

        content = response.content
        # 验证图片
        if not self._validate_image(content):
            return None

        # 保存缓存
        self.file_cache.set(cache_path, content, region="images")
        return content

    async def async_fetch_image(
        self,
        url: str,
        proxy: Optional[bool] = None,
        use_cache: bool = True,
        cookies: Optional[str | dict] = None) -> Optional[bytes]:
        """
        获取图片（异步版本）
        """
        if not url:
            return None

        cache_path = self._prepare_cache_path(url)

        # 检查缓存
        if use_cache:
            content = await self.async_file_cache.get(cache_path, region="images")
            if content:
                return content

        # 请求远程图片
        params = self._get_request_params(url, proxy, cookies)
        response = await AsyncRequestUtils(**params).get_res(url=url)
        if response is None or response.status_code != 200:
            logger.warn(f"Failed to fetch image from URL: {url}")
            return None

        content = response.content
        # 验证图片
        if not self._validate_image(content):
            return None

        # 保存缓存
        await self.async_file_cache.set(cache_path, content, region="images")
        return content

    async def async_fetch_image_guarded(
        self,
        url: str,
        *,
        redirect_validator: Callable[[str], Awaitable[bool]],
        max_bytes: int,
        max_pixels: int,
        proxy: Optional[bool] = None,
        use_cache: bool = True,
        cookies: Optional[str | dict] = None,
        max_redirects: int = 3,
    ) -> Optional[bytes]:
        """
        以有界流式请求抓取未登录可访问的图片。

        每个重定向目标必须重新通过调用方的安全校验；跨主机跳转不会携带原始
        Cookie。字节和像素上限在写入共享图片缓存前生效，避免异常来源污染缓存。
        """
        if not url or max_bytes <= 0 or max_pixels <= 0:
            return None

        cache_path = self._prepare_cache_path(url)
        if use_cache:
            content = await self.async_file_cache.get(cache_path, region="images")
            if content:
                if len(content) <= max_bytes and self._validate_image(content, max_pixels):
                    return content
                await self.async_file_cache.delete(cache_path, region="images")

        source_host = (urlparse(url).hostname or "").lower()
        current_url = url
        redirects = 0

        while True:
            current_host = (urlparse(current_url).hostname or "").lower()
            request_cookies = cookies if current_host == source_host else None
            params = self._get_request_params(current_url, proxy, request_cookies)
            request = AsyncRequestUtils(**params, follow_redirects=False)

            async with request.get_stream(current_url) as response:
                if response is None:
                    return None

                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location or redirects >= max_redirects:
                        return None
                    next_url = urljoin(current_url, location)
                    if not await redirect_validator(next_url):
                        return None
                    current_url = next_url
                    redirects += 1
                    continue

                if response.status_code != 200:
                    logger.warning(
                        "登录壁纸抓取失败，状态码: %s",
                        response.status_code,
                    )
                    return None

                content_length = response.headers.get("content-length")
                if content_length:
                    try:
                        if int(content_length) > max_bytes:
                            return None
                    except ValueError:
                        pass

                payload = bytearray()
                async for chunk in response.aiter_bytes():
                    payload.extend(chunk)
                    if len(payload) > max_bytes:
                        return None
                break

        content = bytes(payload)
        if not self._validate_image(content, max_pixels):
            return None
        if use_cache:
            await self.async_file_cache.set(cache_path, content, region="images")
        return content
