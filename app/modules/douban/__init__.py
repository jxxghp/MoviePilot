import time
from pathlib import Path
from typing import List, Optional, Tuple, Union
from xml.dom import minidom

from app.core import MediaInfo, settings, MetaInfo
from app.core.meta import MetaBase
from app.log import logger
from app.modules import _ModuleBase
from app.modules.douban.apiv2 import DoubanApi
from app.utils.dom import DomUtils
from app.utils.http import RequestUtils
from app.utils.system import SystemUtils
from app.utils.types import MediaType


class Douban(_ModuleBase):

    def __init__(self):
        super().__init__()
        self.doubanapi = DoubanApi()

    def init_module(self) -> None:
        pass

    def stop(self):
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
            if meta.get_name() == name and (not season or meta.begin_season == season):
                return item_obj
        return {}

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
                if not meta.get_name():
                    continue
                # 根据名称查询豆瓣数据
                doubaninfo = self.match(name=mediainfo.title, year=mediainfo.year, season=meta.begin_season)
                if not doubaninfo:
                    logger.warn(f"未找到 {mediainfo.title} 的豆瓣信息")
                    break
                doubaninfo = self.__extend_doubaninfo(doubaninfo)
                # 刮削
                self.gen_scraper_files(meta, doubaninfo, file)
            except Exception as e:
                logger.error(f"刮削文件 {file} 失败，原因：{e}")
            logger.info(f"{file} 刮削完成")

    def gen_scraper_files(self, meta: MetaBase, doubaninfo: dict, file_path: Path):
        """
        生成刮削文件
        :param meta: 元数据
        :param doubaninfo: 豆瓣信息
        :param file_path: 文件路径
        """

        try:
            # 电影
            if meta.type == MediaType.MOVIE:
                # 强制或者不已存在时才处理
                if not file_path.with_name("movie.nfo").exists() \
                        and not file_path.with_suffix(".nfo").exists():
                    #  生成电影描述文件
                    self.__gen_movie_nfo_file(doubaninfo=doubaninfo,
                                              file_path=file_path)
                # 生成电影图片
                self.__save_image(url=doubaninfo.get('poster_path'),
                                  file_path=file_path.with_name(f"poster{Path(doubaninfo.get('poster_path')).suffix}"))
            # 电视剧
            else:
                # 不存在时才处理
                if not file_path.parent.with_name("tvshow.nfo").exists():
                    # 根目录描述文件
                    self.__gen_tv_nfo_file(doubaninfo=doubaninfo,
                                           dir_path=file_path.parents[1])
                # 生成根目录图片
                self.__save_image(url=doubaninfo.get('poster_path'),
                                  file_path=file_path.with_name(f"poster{Path(doubaninfo.get('poster_path')).suffix}"))
                # 季目录NFO
                if not file_path.with_name("season.nfo").exists():
                    self.__gen_tv_season_nfo_file(seasoninfo=doubaninfo,
                                                  season=meta.begin_season,
                                                  season_path=file_path.parent)
        except Exception as e:
            logger.error(f"{file_path} 刮削失败：{e}")

    @staticmethod
    def __gen_common_nfo(doubaninfo: dict, doc, root):
        # 添加时间
        DomUtils.add_node(doc, root, "dateadded",
                          time.strftime('%Y-%m-%d %H:%M:%S',
                                        time.localtime(time.time())))
        # 简介
        xplot = DomUtils.add_node(doc, root, "plot")
        xplot.appendChild(doc.createCDATASection(doubaninfo.get('overview') or ""))
        xoutline = DomUtils.add_node(doc, root, "outline")
        xoutline.appendChild(doc.createCDATASection(doubaninfo.get('.overview') or ""))
        # 导演
        for director in doubaninfo.get('directors'):
            DomUtils.add_node(doc, root, "director", director.get("name") or "")
        # 演员
        for actor in doubaninfo.get('actors'):
            xactor = DomUtils.add_node(doc, root, "actor")
            DomUtils.add_node(doc, xactor, "name", actor.get("name") or "")
            DomUtils.add_node(doc, xactor, "type", "Actor")
            DomUtils.add_node(doc, xactor, "role", actor.get("character") or actor.get("role") or "")
            DomUtils.add_node(doc, xactor, "thumb", actor.get('avatar', {}).get('normal'))
            DomUtils.add_node(doc, xactor, "profile", actor.get('url'))
        # 评分
        DomUtils.add_node(doc, root, "rating", doubaninfo.get('vote_average') or "0")

        return doc

    def __gen_movie_nfo_file(self,
                             doubaninfo: dict,
                             file_path: Path):
        """
        生成电影的NFO描述文件
        :param doubaninfo: 豆瓣信息
        :param file_path: 电影文件路径
        """
        # 开始生成XML
        logger.info(f"正在生成电影NFO文件：{file_path.name}")
        doc = minidom.Document()
        root = DomUtils.add_node(doc, doc, "movie")
        # 公共部分
        doc = self.__gen_common_nfo(doubaninfo=doubaninfo,
                                    doc=doc,
                                    root=root)
        # 标题
        DomUtils.add_node(doc, root, "title", doubaninfo.get('title') or "")
        # 年份
        DomUtils.add_node(doc, root, "year", doubaninfo.get('year') or "")
        # 保存
        self.__save_nfo(doc, file_path.with_suffix(".nfo"))

    def __gen_tv_nfo_file(self,
                          doubaninfo: dict,
                          dir_path: Path):
        """
        生成电视剧的NFO描述文件
        :param doubaninfo: 媒体信息
        :param dir_path: 电视剧根目录
        """
        # 开始生成XML
        logger.info(f"正在生成电视剧NFO文件：{dir_path.name}")
        doc = minidom.Document()
        root = DomUtils.add_node(doc, doc, "tvshow")
        # 公共部分
        doc = self.__gen_common_nfo(doubaninfo=doubaninfo,
                                    doc=doc,
                                    root=root)
        # 标题
        DomUtils.add_node(doc, root, "title", doubaninfo.get('title') or "")
        # 年份
        DomUtils.add_node(doc, root, "year", doubaninfo.get('year') or "")
        DomUtils.add_node(doc, root, "season", "-1")
        DomUtils.add_node(doc, root, "episode", "-1")
        # 保存
        self.__save_nfo(doc, dir_path.joinpath("tvshow.nfo"))

    def __gen_tv_season_nfo_file(self, seasoninfo: dict, season: int, season_path: Path):
        """
        生成电视剧季的NFO描述文件
        :param seasoninfo: TMDB季媒体信息
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
        xplot.appendChild(doc.createCDATASection(seasoninfo.get("overview") or ""))
        xoutline = DomUtils.add_node(doc, root, "outline")
        xoutline.appendChild(doc.createCDATASection(seasoninfo.get("overview") or ""))
        # 标题
        DomUtils.add_node(doc, root, "title", "季 %s" % season)
        # 发行日期
        DomUtils.add_node(doc, root, "premiered", seasoninfo.get("air_date") or "")
        DomUtils.add_node(doc, root, "releasedate", seasoninfo.get("air_date") or "")
        # 发行年份
        DomUtils.add_node(doc, root, "year", seasoninfo.get("air_date")[:4] if seasoninfo.get("air_date") else "")
        # seasonnumber
        DomUtils.add_node(doc, root, "seasonnumber", str(season))
        # 保存
        self.__save_nfo(doc, season_path.joinpath("season.nfo"))

    def __gen_tv_episode_nfo_file(self,
                                  episodeinfo: dict,
                                  season: int,
                                  episode: int,
                                  file_path: Path,
                                  force_nfo: bool = False):
        """
        生成电视剧集的NFO描述文件
        :param episodeinfo: 集TMDB元数据
        :param season: 季号
        :param episode: 集号
        :param file_path: 集文件的路径
        :param force_nfo: 是否强制生成NFO文件
        """
        if not force_nfo and file_path.with_suffix(".nfo").exists():
            return
        # 开始生成集的信息
        logger.info(f"正在生成剧集NFO文件：{file_path.name}")
        doc = minidom.Document()
        root = DomUtils.add_node(doc, doc, "episodedetails")
        # 添加时间
        DomUtils.add_node(doc, root, "dateadded", time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(time.time())))
        # TMDBID
        uniqueid = DomUtils.add_node(doc, root, "uniqueid", episodeinfo.get("id") or "")
        uniqueid.setAttribute("type", "tmdb")
        uniqueid.setAttribute("default", "true")
        # tmdbid
        DomUtils.add_node(doc, root, "tmdbid", episodeinfo.get("id") or "")
        # 标题
        DomUtils.add_node(doc, root, "title", episodeinfo.get("name") or "第 %s 集" % episode)
        # 简介
        xplot = DomUtils.add_node(doc, root, "plot")
        xplot.appendChild(doc.createCDATASection(episodeinfo.get("overview") or ""))
        xoutline = DomUtils.add_node(doc, root, "outline")
        xoutline.appendChild(doc.createCDATASection(episodeinfo.get("overview") or ""))
        # 发布日期
        DomUtils.add_node(doc, root, "aired", episodeinfo.get("air_date") or "")
        # 年份
        DomUtils.add_node(doc, root, "year",
                          episodeinfo.get("air_date")[:4] if episodeinfo.get("air_date") else "")
        # 季
        DomUtils.add_node(doc, root, "season", str(season))
        # 集
        DomUtils.add_node(doc, root, "episode", str(episode))
        # 评分
        DomUtils.add_node(doc, root, "rating", episodeinfo.get("vote_average") or "0")
        # 导演
        directors = episodeinfo.get("crew") or []
        for director in directors:
            if director.get("known_for_department") == "Directing":
                xdirector = DomUtils.add_node(doc, root, "director", director.get("name") or "")
                xdirector.setAttribute("tmdbid", str(director.get("id") or ""))
        # 演员
        actors = episodeinfo.get("guest_stars") or []
        for actor in actors:
            if actor.get("known_for_department") == "Acting":
                xactor = DomUtils.add_node(doc, root, "actor")
                DomUtils.add_node(doc, xactor, "name", actor.get("name") or "")
                DomUtils.add_node(doc, xactor, "type", "Actor")
                DomUtils.add_node(doc, xactor, "tmdbid", actor.get("id") or "")
        # 保存文件
        self.__save_nfo(doc, file_path.with_suffix(".nfo"))

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
                # 下载到temp目录，远程则先存到temp再远程移动，本地则直接保存
                logger.info(f"图片已保存：{file_path.name}")
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
        logger.info(f"NFO文件已保存：{file_path.name}")
