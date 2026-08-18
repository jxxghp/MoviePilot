"""远程图片的抓取、缓存与格式识别适配。"""

import io
from pathlib import Path
from typing import Optional

from PIL import Image

from app.adapters.network.http import RequestUtils, AsyncRequestUtils
from app.adapters.network.ip import IpUtils
from app.foundation.singleton import Singleton
from app.foundation.url import sanitize_path
from app.runtime.cache import FileCache, AsyncFileCache
from app.runtime.config import settings
from app.runtime.log import logger


class ImageHelper(metaclass=Singleton):
    """统一管理同步和异步图片缓存。"""

    def __init__(self):
        """按全局图片缓存天数初始化文件缓存。"""
        _base_path = settings.CACHE_PATH
        _ttl = settings.GLOBAL_IMAGE_CACHE_DAYS * 24 * 3600
        self.file_cache = FileCache(base=_base_path, ttl=_ttl)
        self.async_file_cache = AsyncFileCache(base=_base_path, ttl=_ttl)

    @staticmethod
    def _prepare_cache_path(url: str) -> str:
        """缓存路径"""
        sanitized_path = sanitize_path(url)
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
