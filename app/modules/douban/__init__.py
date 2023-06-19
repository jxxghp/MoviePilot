import time
from pathlib import Path
from typing import List, Optional, Tuple, Union
from xml.dom import minidom

from app.core.context import MediaInfo
from app.core.config import settings
from app.core.metainfo import MetaInfo
from app.core.meta import MetaBase
from app.log import logger
from app.modules import _ModuleBase
from app.modules.douban.apiv2 import DoubanApi
from app.utils.dom import DomUtils
from app.utils.http import RequestUtils
from app.utils.system import SystemUtils
from app.schemas.types import MediaType


class DoubanModule(_ModuleBase):

    doubanapi: DoubanApi = None

    def init_module(self) -> None:
        self.doubanapi = DoubanApi()

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
                        start: int = 0, count: int = 30) -> Optional[List[dict]]:
        """
        发现豆瓣电影、剧集
        :param mtype:  媒体类型
        :param sort:  排序方式
        :param tags:  标签
        :param start:  起始位置
        :param count:  数量
        :return: 媒体信息列表
        """
        logger.info(f"开始发现豆瓣 {mtype.value} ...")
        if mtype == MediaType.MOVIE:
            infos = self.doubanapi.movie_recommend(start=start, count=count,
                                                   sort=sort, tags=tags)
        else:
            infos = self.doubanapi.tv_recommend(start=start, count=count,
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

    def match(self, name: str, year: str, season: int = None) -> dict:
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
        # 目录下的所有文件
        for file in SystemUtils.list_files_with_extensions(path, settings.RMT_MEDIAEXT):
            if not file:
                continue
            logger.info(f"开始刮削媒体库文件：{file} ...")
            try:
                meta = MetaInfo(file.stem)
                if not meta.name:
                    continue
                # 根据名称查询豆瓣数据
                doubaninfo = self.match(name=mediainfo.title, year=mediainfo.year, season=meta.begin_season)
                if not doubaninfo:
                    logger.warn(f"未找到 {mediainfo.title} 的豆瓣信息")
                    break
                # 刮削
                self.gen_scraper_files(meta, MediaInfo(douban_info=doubaninfo), file)
            except Exception as e:
                logger.error(f"刮削文件 {file} 失败，原因：{e}")
            logger.info(f"{file} 刮削完成")

    def gen_scraper_files(self, meta: MetaBase, mediainfo: MediaInfo, file_path: Path):
        """
        生成刮削文件
        :param meta: 元数据
        :param mediainfo: 媒体信息
        :param file_path: 文件路径
        """

        try:
            # 电影
            if mediainfo.type == MediaType.MOVIE:
                # 强制或者不已存在时才处理
                if not file_path.with_name("movie.nfo").exists() \
                        and not file_path.with_suffix(".nfo").exists():
                    #  生成电影描述文件
                    self.__gen_movie_nfo_file(mediainfo=mediainfo,
                                              file_path=file_path)
                # 生成电影图片
                self.__save_image(url=mediainfo.poster_path,
                                  file_path=file_path.with_name(f"poster{Path(mediainfo.poster_path).suffix}"))
            # 电视剧
            else:
                # 不存在时才处理
                if not file_path.parent.with_name("tvshow.nfo").exists():
                    # 根目录描述文件
                    self.__gen_tv_nfo_file(mediainfo=mediainfo,
                                           dir_path=file_path.parents[1])
                # 生成根目录图片
                self.__save_image(url=mediainfo.poster_path,
                                  file_path=file_path.with_name(f"poster{Path(mediainfo.poster_path).suffix}"))
                # 季目录NFO
                if not file_path.with_name("season.nfo").exists():
                    self.__gen_tv_season_nfo_file(mediainfo=mediainfo,
                                                  season=meta.begin_season,
                                                  season_path=file_path.parent)
        except Exception as e:
            logger.error(f"{file_path} 刮削失败：{e}")

    @staticmethod
    def __gen_common_nfo(mediainfo: MediaInfo, doc, root):
        # 添加时间
        DomUtils.add_node(doc, root, "dateadded",
                          time.strftime('%Y-%m-%d %H:%M:%S',
                                        time.localtime(time.time())))
        # 简介
        xplot = DomUtils.add_node(doc, root, "plot")
        xplot.appendChild(doc.createCDATASection(mediainfo.overview or ""))
        xoutline = DomUtils.add_node(doc, root, "outline")
        xoutline.appendChild(doc.createCDATASection(mediainfo.overview or ""))
        # 导演
        for director in mediainfo.directors:
            DomUtils.add_node(doc, root, "director", director.get("name") or "")
        # 演员
        for actor in mediainfo.actors:
            xactor = DomUtils.add_node(doc, root, "actor")
            DomUtils.add_node(doc, xactor, "name", actor.get("name") or "")
            DomUtils.add_node(doc, xactor, "type", "Actor")
            DomUtils.add_node(doc, xactor, "role", actor.get("character") or actor.get("role") or "")
            DomUtils.add_node(doc, xactor, "thumb", actor.get('avatar', {}).get('normal'))
            DomUtils.add_node(doc, xactor, "profile", actor.get('url'))
        # 评分
        DomUtils.add_node(doc, root, "rating", mediainfo.vote_average or "0")

        return doc

    def __gen_movie_nfo_file(self,
                             mediainfo: MediaInfo,
                             file_path: Path):
        """
        生成电影的NFO描述文件
        :param mediainfo: 豆瓣信息
        :param file_path: 电影文件路径
        """
        # 开始生成XML
        logger.info(f"正在生成电影NFO文件：{file_path.name}")
        doc = minidom.Document()
        root = DomUtils.add_node(doc, doc, "movie")
        # 公共部分
        doc = self.__gen_common_nfo(mediainfo=mediainfo,
                                    doc=doc,
                                    root=root)
        # 标题
        DomUtils.add_node(doc, root, "title", mediainfo.title or "")
        # 年份
        DomUtils.add_node(doc, root, "year", mediainfo.year or "")
        # 保存
        self.__save_nfo(doc, file_path.with_suffix(".nfo"))

    def __gen_tv_nfo_file(self,
                          mediainfo: MediaInfo,
                          dir_path: Path):
        """
        生成电视剧的NFO描述文件
        :param mediainfo: 媒体信息
        :param dir_path: 电视剧根目录
        """
        # 开始生成XML
        logger.info(f"正在生成电视剧NFO文件：{dir_path.name}")
        doc = minidom.Document()
        root = DomUtils.add_node(doc, doc, "tvshow")
        # 公共部分
        doc = self.__gen_common_nfo(mediainfo=mediainfo,
                                    doc=doc,
                                    root=root)
        # 标题
        DomUtils.add_node(doc, root, "title", mediainfo.title or "")
        # 年份
        DomUtils.add_node(doc, root, "year", mediainfo.year or "")
        DomUtils.add_node(doc, root, "season", "-1")
        DomUtils.add_node(doc, root, "episode", "-1")
        # 保存
        self.__save_nfo(doc, dir_path.joinpath("tvshow.nfo"))

    def __gen_tv_season_nfo_file(self, mediainfo: MediaInfo, season: int, season_path: Path):
        """
        生成电视剧季的NFO描述文件
        :param mediainfo: 媒体信息
        :param season: 季号
        :param season_path: 电视剧季的目录
        """
        logger.info(f"正在生成季NFO文件：{season_path.name}")
        doc = minidom.Document()
        root = DomUtils.add_node(doc, doc, "season")
        # 添加时间
        DomUtils.add_node(doc, root, "dateadded", time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(time.time())))
        # 简介
        xplot = DomUtils.add_node(doc, root, "plot")
        xplot.appendChild(doc.createCDATASection(mediainfo.overview or ""))
        xoutline = DomUtils.add_node(doc, root, "outline")
        xoutline.appendChild(doc.createCDATASection(mediainfo.overview or ""))
        # 标题
        DomUtils.add_node(doc, root, "title", "季 %s" % season)
        # 发行日期
        DomUtils.add_node(doc, root, "premiered", mediainfo.release_date or "")
        DomUtils.add_node(doc, root, "releasedate", mediainfo.release_date or "")
        # 发行年份
        DomUtils.add_node(doc, root, "year", mediainfo.release_date[:4] if mediainfo.release_date else "")
        # seasonnumber
        DomUtils.add_node(doc, root, "seasonnumber", str(season))
        # 保存
        self.__save_nfo(doc, season_path.joinpath("season.nfo"))

    @staticmethod
    def __save_image(url: str, file_path: Path):
        """
        下载图片并保存
        """
        if file_path.exists():
            return
        try:
            logger.info(f"正在下载{file_path.stem}图片：{url} ...")
            r = RequestUtils().get_res(url=url)
            if r:
                file_path.write_bytes(r.content)
                logger.info(f"图片已保存：{file_path}")
            else:
                logger.info(f"{file_path.stem}图片下载失败，请检查网络连通性")
        except Exception as err:
            logger.error(f"{file_path.stem}图片下载失败：{err}")

    @staticmethod
    def __save_nfo(doc, file_path: Path):
        """
        保存NFO
        """
        if file_path.exists():
            return
        xml_str = doc.toprettyxml(indent="  ", encoding="utf-8")
        file_path.write_bytes(xml_str)
        logger.info(f"NFO文件已保存：{file_path}")
