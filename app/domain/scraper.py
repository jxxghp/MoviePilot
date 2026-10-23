"""
读取 Kodi 风格媒体 NFO，并按标准媒体信息构造元数据文档和图片清单。
"""

import xml.etree.ElementTree as ET
from pathlib import Path
from typing import List, Optional
from urllib.parse import urlparse
from xml.dom import minidom

from app.domain.context import MediaInfo
from app.foundation.dom import DomUtils
from app.schemas.types import MediaSource, MediaType


class NfoReader:
    """
    读取 Kodi 风格 NFO XML 中的元素值。
    """

    def __init__(self, xml_file_path: Path) -> None:
        """
        解析指定 NFO 文件并保存根元素。
        """
        self.xml_file_path = xml_file_path
        self.tree = ET.parse(xml_file_path)
        self.root = self.tree.getroot()

    def get_element_value(self, element_path: str) -> Optional[str]:
        """
        返回首个匹配元素的文本。
        """
        element = self.root.find(element_path)
        return element.text if element is not None else None

    def get_elements(self, element_path: str) -> List[ET.Element]:
        """
        返回路径匹配的全部 XML 元素。
        """
        return self.root.findall(element_path)


class MediaScraperHelper:
    """
    基于统一媒体信息生成通用 NFO 与图片清单，供缺少专用刮削格式的数据源复用。
    """

    @staticmethod
    def _media_identity(
        mediainfo: MediaInfo,
        season: Optional[int] = None,
        episode: Optional[int] = None,
    ) -> tuple[Optional[MediaSource], Optional[str]]:
        """
        获取媒体信息中的来源与来源原生 ID。

        :param mediainfo: 统一媒体信息
        :param season: 单集所属季号；主媒体或季元数据时为空
        :param episode: 单集集号；为空时使用主媒体身份
        :return: 数据源名称与原生 ID；IMDb 单集缺少独立身份时 ID 为 None，不使用剧集主 ID
        """
        if mediainfo.media_source == MediaSource.IMDb and season is not None and episode is not None:
            for detail in mediainfo.season_info:
                if detail.get("season_number") != season:
                    continue
                for item in detail.get("episodes") or []:
                    if item.get("episode_number") == episode:
                        return MediaSource.IMDb, item.get("id")
            return MediaSource.IMDb, None
        return mediainfo.media_source, mediainfo.media_id

    @staticmethod
    def _image_extension(url: str) -> str:
        """
        从图片 URL 中提取可用于本地文件名的扩展名。

        :param url: 图片地址
        :return: 图片扩展名，无法确定时返回 .jpg
        """
        extension = Path(urlparse(url).path).suffix.lower()
        return extension if extension in {".jpg", ".jpeg", ".png", ".webp"} else ".jpg"

    @classmethod
    def _append_common_nodes(
        cls,
        mediainfo: MediaInfo,
        doc: minidom.Document,
        root: minidom.Element,
        season: Optional[int] = None,
        episode: Optional[int] = None,
    ) -> None:
        """
        向 NFO 根节点写入各媒体类型共享的标准字段。

        :param mediainfo: 统一媒体信息
        :param doc: XML 文档
        :param root: NFO 根节点
        :param season: 单集季号，用于选择来源原生身份
        :param episode: 单集集号，不使用剧集主身份代替单集
        """
        DomUtils.add_node(doc, root, "title", mediainfo.title or "")
        DomUtils.add_node(doc, root, "originaltitle", mediainfo.original_title or "")
        DomUtils.add_node(doc, root, "year", mediainfo.year or "")
        DomUtils.add_node(doc, root, "premiered", mediainfo.release_date or "")
        DomUtils.add_node(doc, root, "rating", mediainfo.vote_average or "0")

        plot = DomUtils.add_node(doc, root, "plot")
        plot.appendChild(doc.createCDATASection(mediainfo.overview or ""))
        outline = DomUtils.add_node(doc, root, "outline")
        outline.appendChild(doc.createCDATASection(mediainfo.overview or ""))

        source, media_id = cls._media_identity(mediainfo, season=season, episode=episode)
        if source and media_id:
            unique_id = DomUtils.add_node(doc, root, "uniqueid", media_id)
            unique_id.setAttribute("type", str(source))
            unique_id.setAttribute("default", "true")

        for genre in mediainfo.genres or []:
            genre_name = genre.get("name") if isinstance(genre, dict) else str(genre)
            if genre_name:
                DomUtils.add_node(doc, root, "genre", genre_name)

        for company in mediainfo.production_companies or []:
            company_name = company.get("name") if isinstance(company, dict) else str(company)
            if company_name:
                DomUtils.add_node(doc, root, "studio", company_name)

        for director in mediainfo.directors or []:
            director_name = director.get("name") if isinstance(director, dict) else str(director)
            if director_name:
                DomUtils.add_node(doc, root, "director", director_name)

        for actor in mediainfo.actors or []:
            if not isinstance(actor, dict):
                continue
            actor_node = DomUtils.add_node(doc, root, "actor")
            DomUtils.add_node(doc, actor_node, "name", actor.get("name") or "")
            DomUtils.add_node(
                doc,
                actor_node,
                "role",
                actor.get("character") or actor.get("role") or "",
            )
            avatar = actor.get("avatar") or actor.get("images") or {}
            if isinstance(avatar, dict):
                DomUtils.add_node(
                    doc,
                    actor_node,
                    "thumb",
                    avatar.get("large") or avatar.get("medium") or avatar.get("normal") or "",
                )

    @classmethod
    def get_metadata_nfo(
        cls,
        mediainfo: MediaInfo,
        season: Optional[int] = None,
        episode: Optional[int] = None,
    ) -> Optional[str]:
        """
        根据统一媒体信息生成电影、剧集、季或单集 NFO。

        :param mediainfo: 统一媒体信息
        :param season: 季号
        :param episode: 集号
        :return: UTF-8 编码声明的 NFO XML 文本；媒体信息为空时返回 None
        """
        if not mediainfo:
            return None

        doc = minidom.Document()
        if mediainfo.type == MediaType.MOVIE:
            root = DomUtils.add_node(doc, doc, "movie")
            cls._append_common_nodes(mediainfo, doc, root)
        elif season is not None and episode is not None:
            root = DomUtils.add_node(doc, doc, "episodedetails")
            cls._append_common_nodes(mediainfo, doc, root, season=season, episode=episode)
            DomUtils.add_node(doc, root, "season", str(season))
            DomUtils.add_node(doc, root, "episode", str(episode))
            DomUtils.add_node(
                doc,
                root,
                "showtitle",
                mediainfo.title or "",
            )
        elif season is not None:
            root = DomUtils.add_node(doc, doc, "season")
            cls._append_common_nodes(mediainfo, doc, root)
            DomUtils.add_node(doc, root, "seasonnumber", str(season))
        else:
            root = DomUtils.add_node(doc, doc, "tvshow")
            cls._append_common_nodes(mediainfo, doc, root)
            DomUtils.add_node(doc, root, "season", "-1")
            DomUtils.add_node(doc, root, "episode", "-1")

        return doc.toprettyxml(indent="  ", encoding="utf-8").decode("utf-8")

    @classmethod
    def get_metadata_img(
        cls,
        mediainfo: MediaInfo,
        season: Optional[int] = None,
        episode: Optional[int] = None,
    ) -> dict[str, str]:
        """
        根据统一媒体信息生成主海报和背景图下载清单。

        :param mediainfo: 统一媒体信息
        :param season: 季号
        :param episode: 集号
        :return: 图片文件名与下载地址映射
        """
        if not mediainfo or season is not None or episode is not None:
            return {}
        images = {}
        if mediainfo.poster_path:
            extension = cls._image_extension(mediainfo.poster_path)
            images[f"poster{extension}"] = mediainfo.poster_path
        if mediainfo.backdrop_path:
            extension = cls._image_extension(mediainfo.backdrop_path)
            images[f"backdrop{extension}"] = mediainfo.backdrop_path
        return images
