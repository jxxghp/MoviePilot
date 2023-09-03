from pathlib import Path
from typing import List, Optional, Tuple, Union

from app.core.config import settings
from app.core.context import MediaInfo
from app.core.meta import MetaBase
from app.core.metainfo import MetaInfo
from app.log import logger
from app.modules import _ModuleBase
from app.modules.douban.apiv2 import DoubanApi
from app.modules.douban.scraper import DoubanScraper
from app.schemas.types import MediaType
from app.utils.system import SystemUtils


class DoubanModule(_ModuleBase):

    doubanapi: DoubanApi = None
    scraper: DoubanScraper = None

    def init_module(self) -> None:
        self.doubanapi = DoubanApi()
        self.scraper = DoubanScraper()

    def stop(self):
        pass

    def init_setting(self) -> Tuple[str, Union[str, bool]]:
        pass

    def douban_info(self, doubanid: str) -> Optional[dict]:
        """
        获取豆瓣信息
        :param doubanid: 豆瓣ID
        :return: 豆瓣信息
        """
        if not doubanid:
            return None
        logger.info(f"开始获取豆瓣信息：{doubanid} ...")
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
        return douban_info

    def douban_discover(self, mtype: MediaType, sort: str, tags: str,
                        page: int = 1, count: int = 30) -> Optional[List[dict]]:
        """
        发现豆瓣电影、剧集
        :param mtype:  媒体类型
        :param sort:  排序方式
        :param tags:  标签
        :param page:  页码
        :param count:  数量
        :return: 媒体信息列表
        """
        logger.info(f"开始发现豆瓣 {mtype.value} ...")
        if mtype == MediaType.MOVIE:
            infos = self.doubanapi.movie_recommend(start=(page - 1) * count, count=count,
                                                   sort=sort, tags=tags)
        else:
            infos = self.doubanapi.tv_recommend(start=(page - 1) * count, count=count,
                                                sort=sort, tags=tags)
        if not infos:
            return []
        return infos.get("items") or []

    def movie_showing(self, page: int = 1, count: int = 30) -> List[dict]:
        """
        获取正在上映的电影
        """
        infos = self.doubanapi.movie_showing(start=(page - 1) * count,
                                             count=count)
        if not infos:
            return []
        return infos.get("subject_collection_items")

    def tv_weekly_chinese(self, page: int = 1, count: int = 30) -> List[dict]:
        """
        获取豆瓣本周口碑国产剧
        """
        infos = self.doubanapi.tv_chinese_best_weekly(start=(page - 1) * count,
                                                      count=count)
        if not infos:
            return []
        return infos.get("subject_collection_items")

    def tv_weekly_global(self, page: int = 1, count: int = 30) -> List[dict]:
        """
        获取豆瓣本周口碑外国剧
        """
        infos = self.doubanapi.tv_global_best_weekly(start=(page - 1) * count,
                                                     count=count)
        if not infos:
            return []
        return infos.get("subject_collection_items")

    def search_medias(self, meta: MetaBase) -> Optional[List[MediaInfo]]:
        """
        搜索媒体信息
        :param meta:  识别的元数据
        :reutrn: 媒体信息
        """
        # 未启用豆瓣搜索时返回None
        if settings.SEARCH_SOURCE != "douban":
            return None

        if not meta.name:
            return []
        result = self.doubanapi.search(meta.name)
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

    def __match(self, name: str, year: str, season: int = None) -> dict:
        """
        搜索和匹配豆瓣信息
        """
        result = self.doubanapi.search(f"{name} {year or ''}")
        if not result:
            return {}
        for item_obj in result.get("items"):
            if item_obj.get("type_name") not in (MediaType.TV.value, MediaType.MOVIE.value):
                continue
            title = item_obj.get("title")
            if not title:
                continue
            meta = MetaInfo(title)
            if meta.name == name and (not season or meta.begin_season == season):
                return item_obj
        return {}

    def movie_top250(self, page: int = 1, count: int = 30) -> List[dict]:
        """
        获取豆瓣电影TOP250
        """
        infos = self.doubanapi.movie_top250(start=(page - 1) * count,
                                            count=count)
        if not infos:
            return []
        return infos.get("subject_collection_items")

    def scrape_metadata(self, path: Path, mediainfo: MediaInfo) -> None:
        """
        刮削元数据
        :param path: 媒体文件路径
        :param mediainfo:  识别的媒体信息
        :return: 成功或失败
        """
        if settings.SCRAP_SOURCE != "douban":
            return None
        if SystemUtils.is_bluray_dir(path):
            # 蓝光原盘
            logger.info(f"开始刮削蓝光原盘：{path} ...")
            meta = MetaInfo(path.stem)
            if not meta.name:
                return
            # 根据名称查询豆瓣数据
            doubaninfo = self.__match(name=mediainfo.title, year=mediainfo.year, season=meta.begin_season)
            if not doubaninfo:
                logger.warn(f"未找到 {mediainfo.title} 的豆瓣信息")
                return
            scrape_path = path / path.name
            self.scraper.gen_scraper_files(meta=meta,
                                           mediainfo=MediaInfo(douban_info=doubaninfo),
                                           file_path=scrape_path)
        else:
            # 目录下的所有文件
            for file in SystemUtils.list_files(path, settings.RMT_MEDIAEXT):
                if not file:
                    continue
                logger.info(f"开始刮削媒体库文件：{file} ...")
                try:
                    meta = MetaInfo(file.stem)
                    if not meta.name:
                        continue
                    # 根据名称查询豆瓣数据
                    doubaninfo = self.__match(name=mediainfo.title,
                                              year=mediainfo.year,
                                              season=meta.begin_season)
                    if not doubaninfo:
                        logger.warn(f"未找到 {mediainfo.title} 的豆瓣信息")
                        break
                    # 刮削
                    self.scraper.gen_scraper_files(meta=meta,
                                                   mediainfo=MediaInfo(douban_info=doubaninfo),
                                                   file_path=file)
                except Exception as e:
                    logger.error(f"刮削文件 {file} 失败，原因：{e}")
        logger.info(f"{path} 刮削完成")
