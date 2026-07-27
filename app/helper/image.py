import asyncio
import io
import threading
from pathlib import Path
from typing import Awaitable, Callable, Optional, List
from urllib.parse import urljoin

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
        self._guarded_fetch_tasks: dict[tuple, asyncio.Task[Optional[bytes]]] = {}
        self._guarded_fetch_tasks_lock = threading.Lock()

    @staticmethod
    def _prepare_cache_path(url: str) -> str:
        """缓存路径"""
        sanitized_path = SecurityUtils.sanitize_url_path(url)
        cache_path = Path(sanitized_path)
        if not cache_path.suffix:
            cache_path = cache_path.with_suffix(".jpg")
        return cache_path.as_posix()

    @staticmethod
    def _validate_image(content: bytes) -> bool:
        """验证图片"""
        if not content:
            return False
        try:
            Image.open(io.BytesIO(content)).verify()
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
        redirect_policy: str,
        max_bytes: int,
        proxy: Optional[bool] = None,
        use_cache: bool = True,
        max_redirects: int = 3,
    ) -> Optional[bytes]:
        """
        以有界流式请求抓取需要逐跳校验的图片。

        每个重定向目标必须重新通过调用方的安全校验，字节上限和图片有效性检查在
        写入共享缓存前生效。只有抓取策略完全等价的并发调用才共享一次远端抓取：
        合并键包含缓存键、`redirect_policy`、字节上限、代理与重定向上限，避免某
        次调用收到超出自身上限的图片，或沿用他人的重定向授权。

        :param redirect_validator: 逐跳校验重定向目标的协程
        :param redirect_policy: 描述该校验授权范围的稳定标识；`redirect_validator`
            通常是每次请求新建的闭包，无法按对象身份判断等价，由调用方显式声明
        """
        if not url or max_bytes <= 0:
            return None

        cache_path = self._prepare_cache_path(url)
        if use_cache:
            content = await self.async_file_cache.get(cache_path, region="images")
            if content:
                if len(content) <= max_bytes and self._validate_image(content):
                    return content
                await self.async_file_cache.delete(cache_path, region="images")

        task_key = (
            cache_path,
            redirect_policy,
            max_bytes,
            proxy,
            use_cache,
            max_redirects,
        )
        loop = asyncio.get_running_loop()
        with self._guarded_fetch_tasks_lock:
            task = self._guarded_fetch_tasks.get(task_key)
            if task is None or task.get_loop() is not loop:
                task = loop.create_task(
                    self._download_guarded_image(
                        url=url,
                        cache_path=cache_path,
                        redirect_validator=redirect_validator,
                        max_bytes=max_bytes,
                        proxy=proxy,
                        use_cache=use_cache,
                        max_redirects=max_redirects,
                    )
                )
                self._guarded_fetch_tasks[task_key] = task
                task.add_done_callback(
                    lambda completed, key=task_key: self._forget_guarded_fetch_task(
                        key, completed
                    )
                )

        return await asyncio.shield(task)

    def _forget_guarded_fetch_task(
        self, task_key: tuple, task: asyncio.Task[Optional[bytes]]
    ) -> None:
        """抓取完成后只移除仍指向该任务的合并键，避免旧任务清除后继任务。"""
        with self._guarded_fetch_tasks_lock:
            if self._guarded_fetch_tasks.get(task_key) is task:
                self._guarded_fetch_tasks.pop(task_key, None)

    async def _download_guarded_image(
        self,
        *,
        url: str,
        cache_path: str,
        redirect_validator: Callable[[str], Awaitable[bool]],
        max_bytes: int,
        proxy: Optional[bool],
        use_cache: bool,
        max_redirects: int,
    ) -> Optional[bytes]:
        """执行一次受保护图片抓取，并在任务内复查并写入共享缓存。"""
        if use_cache:
            content = await self.async_file_cache.get(cache_path, region="images")
            if content:
                if len(content) <= max_bytes and self._validate_image(content):
                    return content
                await self.async_file_cache.delete(cache_path, region="images")

        current_url = url
        redirects = 0

        while True:
            params = self._get_request_params(current_url, proxy, cookies=None)
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
        if not self._validate_image(content):
            return None
        if use_cache:
            await self.async_file_cache.set(cache_path, content, region="images")
        return content
