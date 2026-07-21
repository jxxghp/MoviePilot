from pathlib import Path
from typing import Optional
from urllib.parse import urlparse
from xml.dom import minidom

from app.core.context import MediaInfo
from app.schemas.types import MediaType
from app.utils.dom import DomUtils


class MediaScraperHelper:
    """
    基于统一媒体信息生成通用 NFO 与图片清单，供缺少专用刮削格式的数据源复用
    """

    @staticmethod
    def _media_identity(mediainfo: MediaInfo) -> tuple[Optional[str], Optional[str]]:
        """
        获取媒体信息中的来源与来源原生 ID。

        :param mediainfo: 统一媒体信息
        :return: 数据源名称与原生 ID
        """
        source_ids = {
            "themoviedb": mediainfo.tmdb_id,
            "douban": mediainfo.douban_id,
            "bangumi": mediainfo.bangumi_id,
            "anilist": mediainfo.anilist_id,
        }
        media_id = source_ids.get(mediainfo.source)
        return mediainfo.source, str(media_id) if media_id is not None else None

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
        root: minidom.Node,
    ) -> None:
        """
        向 NFO 根节点写入各媒体类型共享的标准字段。

        :param mediainfo: 统一媒体信息
        :param doc: XML 文档
        :param root: NFO 根节点
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

        source, media_id = cls._media_identity(mediainfo)
        if source and media_id:
            unique_id = DomUtils.add_node(doc, root, "uniqueid", media_id)
            unique_id.setAttribute("type", source)
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
        :return: NFO XML 文本
        """
        if not mediainfo:
            return None

        doc = minidom.Document()
        if mediainfo.type == MediaType.MOVIE:
            root = DomUtils.add_node(doc, doc, "movie")
            cls._append_common_nodes(mediainfo, doc, root)
        elif season is not None and episode is not None:
            root = DomUtils.add_node(doc, doc, "episodedetails")
            cls._append_common_nodes(mediainfo, doc, root)
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

        return doc.toprettyxml(indent="  ", encoding="utf-8")

    @classmethod
    def get_metadata_img(
        cls,
        mediainfo: MediaInfo,
        season: Optional[int] = None,
        episode: Optional[int] = None,
    ) -> dict:
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
