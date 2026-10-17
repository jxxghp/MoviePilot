"""
依据豆瓣媒体信息生成 NFO XML 和主海报、背景图片地址清单。
"""

from pathlib import Path
from typing import Optional
from xml.dom import minidom

from app.domain.context import MediaInfo
from app.foundation.dom import DomUtils
from app.schemas.types import MediaType


class DoubanScraper:
    """
    根据豆瓣媒体信息生成 NFO 与图片清单。
    """

    _force_nfo = False
    _force_img = False

    def get_metadata_nfo(self, mediainfo: MediaInfo, season: Optional[int] = None) -> Optional[str]:
        """
        生成电影、整剧或季级别的 NFO XML。

        :param mediainfo: 包含豆瓣简介、演职员和评分的媒体对象
        :param season: 季号；非电影且提供季号时生成季文档
        :return: 包含 UTF-8 编码声明的 XML 文本；无法生成时返回 None
        """
        if mediainfo.type == MediaType.MOVIE:
            doc = self.__gen_movie_nfo_file(mediainfo=mediainfo)
        else:
            if season is not None:
                doc = self.__gen_tv_season_nfo_file(mediainfo=mediainfo, season=season)
            else:
                doc = self.__gen_tv_nfo_file(mediainfo=mediainfo)
        if doc:
            return doc.toprettyxml(indent="  ", encoding="utf-8").decode("utf-8")

        return None

    @staticmethod
    def get_metadata_img(mediainfo: MediaInfo, season: Optional[int] = None, episode: Optional[int] = None) -> dict[str, str]:
        """
        生成主媒体图片文件名到原始地址的映射。

        :param mediainfo: 提供海报与背景图片路径的媒体对象
        :param season: 豆瓣不提供季图片；非 None 时返回空字典
        :param episode: 豆瓣不提供单集图片；非零时返回空字典
        :return: 已有主海报及背景图的文件名与地址；没有图片时为空字典
        """
        ret_dict = {}
        if season is not None:
            return {}
        if episode:
            return {}
        if mediainfo.poster_path:
            ret_dict[f"poster{Path(mediainfo.poster_path).suffix}"] = mediainfo.poster_path
        if mediainfo.backdrop_path:
            ret_dict[f"backdrop{Path(mediainfo.backdrop_path).suffix}"] = mediainfo.backdrop_path
        return ret_dict

    @staticmethod
    def __gen_common_nfo(mediainfo: MediaInfo, doc: minidom.Document, root: minidom.Element) -> minidom.Document:
        """
        向已有文档追加豆瓣简介、演职员和评分信息。
        """
        xplot = DomUtils.add_node(doc, root, "plot")
        xplot.appendChild(doc.createCDATASection(mediainfo.overview or ""))
        xoutline = DomUtils.add_node(doc, root, "outline")
        xoutline.appendChild(doc.createCDATASection(mediainfo.overview or ""))
        for director in mediainfo.directors:
            DomUtils.add_node(doc, root, "director", director.get("name") or "")
        for actor in mediainfo.actors:
            xactor = DomUtils.add_node(doc, root, "actor")
            DomUtils.add_node(doc, xactor, "name", actor.get("name") or "")
            DomUtils.add_node(doc, xactor, "type", "Actor")
            DomUtils.add_node(doc, xactor, "role", actor.get("character") or actor.get("role") or "")
            DomUtils.add_node(doc, xactor, "thumb", actor.get('avatar', {}).get('normal'))
            DomUtils.add_node(doc, xactor, "profile", actor.get('url'))
        DomUtils.add_node(doc, root, "rating", mediainfo.vote_average or "0")

        return doc

    def __gen_movie_nfo_file(self, mediainfo: MediaInfo) -> minidom.Document:
        """
        生成电影的NFO描述文件。

        :param mediainfo: 豆瓣信息
        """
        doc = minidom.Document()
        root = DomUtils.add_node(doc, doc, "movie")
        doc = self.__gen_common_nfo(mediainfo=mediainfo,
                                    doc=doc,
                                    root=root)
        DomUtils.add_node(doc, root, "title", mediainfo.title or "")
        DomUtils.add_node(doc, root, "year", mediainfo.year or "")

        return doc

    def __gen_tv_nfo_file(self, mediainfo: MediaInfo) -> minidom.Document:
        """
        生成电视剧的NFO描述文件。

        :param mediainfo: 媒体信息
        """
        doc = minidom.Document()
        root = DomUtils.add_node(doc, doc, "tvshow")
        doc = self.__gen_common_nfo(mediainfo=mediainfo,
                                    doc=doc,
                                    root=root)
        DomUtils.add_node(doc, root, "title", mediainfo.title or "")
        DomUtils.add_node(doc, root, "year", mediainfo.year or "")
        DomUtils.add_node(doc, root, "season", "-1")
        DomUtils.add_node(doc, root, "episode", "-1")

        return doc

    @staticmethod
    def __gen_tv_season_nfo_file(mediainfo: MediaInfo,
                                 season: int) -> minidom.Document:
        """
        生成电视剧季的NFO描述文件。

        :param mediainfo: 媒体信息
        :param season: 季号
        """
        doc = minidom.Document()
        root = DomUtils.add_node(doc, doc, "season")
        xplot = DomUtils.add_node(doc, root, "plot")
        xplot.appendChild(doc.createCDATASection(mediainfo.overview or ""))
        xoutline = DomUtils.add_node(doc, root, "outline")
        xoutline.appendChild(doc.createCDATASection(mediainfo.overview or ""))
        DomUtils.add_node(doc, root, "title", "季 %s" % season)
        DomUtils.add_node(doc, root, "premiered", mediainfo.release_date or "")
        DomUtils.add_node(doc, root, "releasedate", mediainfo.release_date or "")
        DomUtils.add_node(doc, root, "year", mediainfo.release_date[:4] if mediainfo.release_date else "")
        DomUtils.add_node(doc, root, "seasonnumber", str(season))

        return doc
