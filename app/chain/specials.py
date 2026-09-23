"""Season 0 单集 NFO 的可选跨来源特典播放顺序补全。"""

from typing import Any, Optional, Union
from xml.dom import minidom

from app.chain._contracts import ChainRuntimeMixinHost
from app.domain.context import MediaInfo
from app.runtime.log import logger
from app.runtime.settings import get_runtime_setting
from app.schemas.types import MediaType

_SPECIAL_EPISODE_ORDER_TAGS = (
    ("airsAfterSeason", "airsafter_season"),
    ("airsBeforeSeason", "airsbefore_season"),
    ("airsBeforeEpisode", "airsbefore_episode"),
)


class SpecialEpisodeOrderEnricher:
    """在调用时接收 Chain 模块分发能力，补全特典播放位置。"""

    def __init__(self, host: ChainRuntimeMixinHost) -> None:
        self._host = host

    def enrich(
        self,
        nfo_content: Union[bytes, str],
        mediainfo: Optional[MediaInfo],
        season: Optional[int],
        episode: Optional[int],
    ) -> Union[bytes, str]:
        """仅为符合条件的 Season 0 单集补全位置，失败时保留原始 NFO。"""
        return self._enrich_special_episode_order(nfo_content, mediainfo, season, episode)

    def _enrich_special_episode_order(
        self,
        nfo_content: Union[bytes, str],
        mediainfo: Optional[MediaInfo],
        season: Optional[int],
        episode: Optional[int],
    ) -> Union[bytes, str]:
        """仅为 TMDb 默认编号的 Season 0 单集查询 TVDB 播放位置。"""
        try:
            if (
                not isinstance(nfo_content, (bytes, str))
                or mediainfo is None
                or mediainfo.type != MediaType.TV
                or season != 0
                or episode is None
                or mediainfo.tmdb_id is None
                or mediainfo.episode_group
                or (mediainfo.scrape_source or get_runtime_setting("SCRAP_SOURCE")) != "themoviedb"
                or not get_runtime_setting("TVDB_V4_API_KEY")
            ):
                return nfo_content

            document = minidom.parseString(nfo_content)
            root = document.documentElement
            if root is None or root.tagName != "episodedetails":
                return nfo_content
            children = {
                node.tagName: node
                for node in root.childNodes
                if isinstance(node, minidom.Element)
            }
            if (
                self._nfo_integer(children.get("season")) != season
                or self._nfo_integer(children.get("episode")) != episode
                or all(tag in children for _, tag in _SPECIAL_EPISODE_ORDER_TAGS)
            ):
                return nfo_content

            tmdb_id = self._positive_integer(mediainfo.tmdb_id)
            if tmdb_id is None:
                return nfo_content
            external_ids = self._host.run_module(
                "tmdb_episode_external_ids", tmdbid=tmdb_id, season=season, episode=episode
            )
            if not isinstance(external_ids, dict):
                return nfo_content
            nfo_episode_id = self._nfo_integer(children.get("tmdbid"))
            mapped_episode_id = self._positive_integer(external_ids.get("id"))
            if (
                nfo_episode_id is not None
                and mapped_episode_id is not None
                and nfo_episode_id != mapped_episode_id
            ):
                return nfo_content
            tvdb_id = self._positive_integer(external_ids.get("tvdb_id"))
            if tvdb_id is None:
                return nfo_content
            tvdb_info = self._host.run_module(
                "tvdb_episode_extended", episode_id=tvdb_id
            )
            if not isinstance(tvdb_info, dict):
                return nfo_content
            return self._apply_special_episode_order_to_nfo(
                nfo_content, document, children, tvdb_info
            )
        except Exception as err:
            logger.debug("补全 Season 0 特典播放顺序失败：%s", err)
            return nfo_content

    @staticmethod
    def _positive_integer(value: object) -> Optional[int]:
        """只接受可转换的正整数外部 ID。"""
        if isinstance(value, bool):
            return None
        try:
            number = int(str(value).strip())
        except (TypeError, ValueError):
            return None
        return number if number > 0 else None

    @staticmethod
    def _nfo_integer(node: Optional[minidom.Element]) -> Optional[int]:
        """读取 NFO 的整数节点，缺失或无效时返回空值。"""
        if node is None:
            return None
        try:
            return int("".join(child.nodeValue or "" for child in node.childNodes))
        except ValueError:
            return None

    @staticmethod
    def _apply_special_episode_order_to_nfo(
        nfo_content: Union[bytes, str],
        document: minidom.Document,
        children: dict[str, minidom.Element],
        tvdb_info: dict[str, Any],
    ) -> Union[bytes, str]:
        """只补全缺失的单集播放位置标签，并保持 NFO 返回类型。"""
        root = document.documentElement
        if root is None:
            return nfo_content
        added = False
        for source_field, tag in _SPECIAL_EPISODE_ORDER_TAGS:
            if tag in children or tvdb_info.get(source_field) is None:
                continue
            value = tvdb_info[source_field]
            if isinstance(value, bool):
                continue
            try:
                number = int(str(value).strip())
            except (TypeError, ValueError):
                continue
            if number < 0:
                continue
            node = document.createElement(tag)
            node.appendChild(document.createTextNode(str(number)))
            root.appendChild(node)
            added = True
        if not added:
            return nfo_content
        # 基础 NFO 已缩进，移除解析后保留的缩进节点，避免二次排版插入空行。
        for element in document.getElementsByTagName("*"):
            if not any(child.nodeType == child.ELEMENT_NODE for child in element.childNodes):
                continue
            for child in tuple(element.childNodes):
                if child.nodeType == child.TEXT_NODE and not (child.nodeValue or "").strip():
                    element.removeChild(child)
        if isinstance(nfo_content, bytes):
            return document.toprettyxml(indent="  ", encoding="utf-8")
        return document.toprettyxml(indent="  ")
