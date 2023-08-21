from typing import Optional, List

from app.chain import ChainBase
from app.core.context import Context
from app.core.context import MediaInfo
from app.core.metainfo import MetaInfo
from app.log import logger
from app.schemas import MediaType


class DoubanChain(ChainBase):
    """
    豆瓣处理链
    """

    def recognize_by_doubanid(self, doubanid: str) -> Optional[Context]:
        """
        根据豆瓣ID识别媒体信息
        """
        logger.info(f'开始识别媒体信息，豆瓣ID：{doubanid} ...')
        # 查询豆瓣信息
        doubaninfo = self.douban_info(doubanid=doubanid)
        if not doubaninfo:
            logger.warn(f'未查询到豆瓣信息，豆瓣ID：{doubanid}')
            return None
        return self.recognize_by_doubaninfo(doubaninfo)

    def recognize_by_doubaninfo(self, doubaninfo: dict) -> Optional[Context]:
        """
        根据豆瓣信息识别媒体信息
        """
        # 使用原标题匹配
        meta = MetaInfo(title=doubaninfo.get("original_title") or doubaninfo.get("title"))
        # 处理类型
        if isinstance(doubaninfo.get('media_type'), MediaType):
            meta.type = doubaninfo.get('media_type')
        else:
            meta.type = MediaType.MOVIE if doubaninfo.get("type") == "movie" else MediaType.TV
        # 识别媒体信息
        mediainfo: MediaInfo = self.recognize_media(meta=meta, mtype=meta.type)
        if not mediainfo:
            logger.warn(f'{meta.name} 未识别到TMDB媒体信息')
            return Context(meta_info=meta, media_info=MediaInfo(douban_info=doubaninfo))
        logger.info(f'识别到媒体信息：{mediainfo.type.value} {mediainfo.title_year} {meta.season}')
        mediainfo.set_douban_info(doubaninfo)
        return Context(meta_info=meta, media_info=mediainfo)

    def movie_top250(self, page: int = 1, count: int = 30) -> List[dict]:
        """
        获取豆瓣电影TOP250
        :param page:  页码
        :param count:  每页数量
        """
        return self.run_module("movie_top250", page=page, count=count)

    def movie_showing(self, page: int = 1, count: int = 30) -> List[dict]:
        """
        获取正在上映的电影
        """
        return self.run_module("movie_showing", page=page, count=count)

    def tv_weekly_chinese(self, page: int = 1, count: int = 30) -> List[dict]:
        """
        获取本周中国剧集榜
        """
        return self.run_module("tv_weekly_chinese", page=page, count=count)

    def tv_weekly_global(self, page: int = 1, count: int = 30) -> List[dict]:
        """
        获取本周全球剧集榜
        """
        return self.run_module("tv_weekly_global", page=page, count=count)

    def douban_discover(self, mtype: MediaType, sort: str, tags: str,
                        page: int = 0, count: int = 30) -> Optional[List[dict]]:
        """
        发现豆瓣电影、剧集
        :param mtype:  媒体类型
        :param sort:  排序方式
        :param tags:  标签
        :param page:  页码
        :param count:  数量
        :return: 媒体信息列表
        """
        return self.run_module("douban_discover", mtype=mtype, sort=sort, tags=tags,
                               page=page, count=count)
