from pathlib import Path
from typing import Optional
from xml.dom import minidom

from app.core.context import MediaInfo
from app.schemas.types import MediaType
from app.utils.dom import DomUtils


class DoubanScraper:
    _force_nfo = False
    _force_img = False

    def get_metadata_nfo(self, mediainfo: MediaInfo, season: Optional[int] = None) -> Optional[str]:
        """
        获取NFO文件内容文本
        :param mediainfo: 媒体信息
        :param season: 季号
        """
        if mediainfo.type == MediaType.MOVIE:
            # 电影元数据文件
            doc = self.__gen_movie_nfo_file(mediainfo=mediainfo)
        else:
            if season:
                # 季元数据文件
                doc = self.__gen_tv_season_nfo_file(mediainfo=mediainfo, season=season)
            else:
                # 电视剧元数据文件
                doc = self.__gen_tv_nfo_file(mediainfo=mediainfo)
        if doc:
            return doc.toprettyxml(indent="  ", encoding="utf-8") # noqa

        return None

    @staticmethod
    def get_metadata_img(mediainfo: MediaInfo, season: Optional[int] = None, episode: Optional[int] = None) -> Optional[dict]:
        """
        获取图片内容
        :param mediainfo: 媒体信息
        :param season: 季号
        :param episode: 集号
        """
        ret_dict = {}
        if season:
            # 豆瓣无季图片
            return {}
        if episode:
            # 豆瓣无集图片
            return {}
        if mediainfo.poster_path:
            ret_dict[f"poster{Path(mediainfo.poster_path).suffix}"] = mediainfo.poster_path
        if mediainfo.backdrop_path:
            ret_dict[f"backdrop{Path(mediainfo.backdrop_path).suffix}"] = mediainfo.backdrop_path
        return ret_dict

    @staticmethod
    def __gen_common_nfo(mediainfo: MediaInfo, doc: minidom.Document, root: minidom.Node):
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

    def __gen_movie_nfo_file(self, mediainfo: MediaInfo) -> minidom.Document:
        """
        生成电影的NFO描述文件
        :param mediainfo: 豆瓣信息
        """
        # 开始生成XML
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

        return doc

    def __gen_tv_nfo_file(self, mediainfo: MediaInfo) -> minidom.Document:
        """
        生成电视剧的NFO描述文件
        :param mediainfo: 媒体信息
        """
        # 开始生成XML
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

        return doc

    @staticmethod
    def __gen_tv_season_nfo_file(mediainfo: MediaInfo,
                                 season: int) -> minidom.Document:
        """
        生成电视剧季的NFO描述文件
        :param mediainfo: 媒体信息
        :param season: 季号
        """
        doc = minidom.Document()
        root = DomUtils.add_node(doc, doc, "season")
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

        return doc
