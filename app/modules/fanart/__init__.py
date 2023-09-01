import re
from functools import lru_cache
from typing import Optional, Tuple, Union

from app.core.context import MediaInfo, settings
from app.log import logger
from app.modules import _ModuleBase
from app.utils.http import RequestUtils
from app.schemas.types import MediaType


class FanartModule(_ModuleBase):

    # 代理
    _proxies: dict = settings.PROXY

    # Fanart Api
    _movie_url: str = f'https://webservice.fanart.tv/v3/movies/%s?api_key={settings.FANART_API_KEY}'
    _tv_url: str = f'https://webservice.fanart.tv/v3/tv/%s?api_key={settings.FANART_API_KEY}'

    def init_module(self) -> None:
        pass

    def stop(self):
        pass

    def init_setting(self) -> Tuple[str, Union[str, bool]]:
        return "FANART_API_KEY", True

    def obtain_images(self, mediainfo: MediaInfo) -> Optional[MediaInfo]:
        """
        获取图片
        :param mediainfo:  识别的媒体信息
        :return: 更新后的媒体信息
        """
        if mediainfo.type == MediaType.MOVIE:
            result = self.__request_fanart(mediainfo.type, mediainfo.tmdb_id)
        else:
            result = self.__request_fanart(mediainfo.type, mediainfo.tvdb_id)
        if not result or result.get('status') == 'error':
            logger.warn(f"没有获取到 {mediainfo.title_year} 的Fanart图片数据")
            return
        for name, images in result.items():
            if not images:
                continue
            if not isinstance(images, list):
                continue
            # 按欢迎程度倒排
            images.sort(key=lambda x: int(x.get('likes', 0)), reverse=True)
            # 图片属性xx_path
            image_name = self.__name(name)
            if not mediainfo.get_image(image_name):
                mediainfo.set_image(image_name, images[0].get('url'))

        return mediainfo

    @staticmethod
    def __name(fanart_name: str) -> str:
        """
        转换Fanart图片的名字
        """
        words_to_remove = r'tv|movie|hdmovie|hdtv|show|hd'
        pattern = re.compile(words_to_remove, re.IGNORECASE)
        result = re.sub(pattern, '', fanart_name)
        return result

    @classmethod
    @lru_cache(maxsize=settings.CACHE_CONF.get('fanart'))
    def __request_fanart(cls, media_type: MediaType, queryid: Union[str, int]) -> Optional[dict]:
        if media_type == MediaType.MOVIE:
            image_url = cls._movie_url % queryid
        else:
            image_url = cls._tv_url % queryid
        try:
            ret = RequestUtils(proxies=cls._proxies, timeout=10).get_res(image_url)
            if ret:
                return ret.json()
        except Exception as err:
            logger.error(f"获取{queryid}的Fanart图片失败：{err}")
        return None
