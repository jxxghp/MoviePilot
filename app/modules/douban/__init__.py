from typing import List, Optional, Tuple, Union

from app.core import MediaInfo, settings
from app.core.meta import MetaBase
from app.modules import _ModuleBase
from app.modules.douban.apiv2 import DoubanApi
from app.utils.types import MediaType


class Douban(_ModuleBase):

    def __init__(self):
        super().__init__()
        self.doubanapi = DoubanApi()

    def init_module(self) -> None:
        pass

    def init_setting(self) -> Tuple[str, Union[str, bool]]:
        pass

    def douban_info(self, doubanid: str) -> Optional[dict]:
        """
        获取豆瓣信息
        :param doubanid: 豆瓣ID
        :return: 识别的媒体信息，包括剧集信息
        """
        if not doubanid:
            return None
        douban_info = self.doubanapi.movie_detail(doubanid)
        if douban_info:
            celebrities = self.doubanapi.movie_celebrities(doubanid)
            if celebrities:
                douban_info["directors"] = celebrities.get("directors")
                douban_info["actors"] = celebrities.get("actors")
        else:
            douban_info = self.doubanapi.tv_detail(doubanid)
            celebrities = self.doubanapi.tv_celebrities(doubanid)
            if douban_info and celebrities:
                douban_info["directors"] = celebrities.get("directors")
                douban_info["actors"] = celebrities.get("actors")
        return self.__extend_doubaninfo(douban_info)

    @staticmethod
    def __extend_doubaninfo(doubaninfo: dict):
        """
        补充添加豆瓣信息
        """
        # 类型
        if doubaninfo.get("type") == "movie":
            doubaninfo['media_type'] = MediaType.MOVIE
        elif doubaninfo.get("type") == "tv":
            doubaninfo['media_type'] = MediaType.TV
        else:
            return doubaninfo
        # 评分
        rating = doubaninfo.get('rating')
        if rating:
            doubaninfo['vote_average'] = float(rating.get("value"))
        else:
            doubaninfo['vote_average'] = 0

        # 海报
        if doubaninfo.get("type") == "movie":
            poster_path = doubaninfo.get('cover', {}).get("url")
            if not poster_path:
                poster_path = doubaninfo.get('cover_url')
            if not poster_path:
                poster_path = doubaninfo.get('pic', {}).get("large")
        else:
            poster_path = doubaninfo.get('pic', {}).get("normal")
        if poster_path:
            poster_path = poster_path.replace("s_ratio_poster", "m_ratio_poster")
        doubaninfo['poster_path'] = poster_path

        # 简介
        doubaninfo['overview'] = doubaninfo.get("card_subtitle") or ""

        return doubaninfo

    def search_medias(self, meta: MetaBase) -> Optional[List[MediaInfo]]:
        """
        搜索媒体信息
        :param meta:  识别的元数据
        :reutrn: 媒体信息
        """
        # 未启用豆瓣搜索时返回None
        if settings.SEARCH_SOURCE != "douban":
            return None

        if not meta.get_name():
            return []
        result = self.doubanapi.search(meta.get_name())
        if not result:
            return []
        # 返回数据
        ret_medias = []
        for item_obj in result.get("items"):
            if meta.type and meta.type.value != item_obj.get("type_name"):
                continue
            if item_obj.get("type_name") not in (MediaType.TV.value, MediaType.MOVIE.value):
                continue
            ret_medias.append(MediaInfo(douban_info=item_obj.get("target")))

        return ret_medias

    def scrape_metadata(self, path: str, mediainfo: MediaInfo) -> None:
        """
        TODO 刮削元数据
        :param path: 媒体文件路径
        :param mediainfo:  识别的媒体信息
        :return: 成功或失败
        """
        if settings.SCRAP_SOURCE != "douban":
            return None
