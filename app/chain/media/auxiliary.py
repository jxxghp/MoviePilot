"""媒体辅助来源聚合与补充 owner。"""

from typing import Iterable, Optional, Union

from app.application.configuration import get_chain_runtime_config_snapshot
from app.chain.media.contract import _MediaOwnerBase
from app.domain.context import (
    MediaInfo,
    MusicInfo,
)
from app.domain.media import parse_media_source_selection
from app.domain.meta.metabase import MetaBase
from app.domain.metainfo import MetaInfo
from app.runtime.log import logger
from app.schemas.media import normalize_media_source
from app.schemas.types import (
    MediaSource,
    MediaSourceSelection,
    MediaType,
)


class MediaAuxiliaryOwner(_MediaOwnerBase):
    """媒体辅助来源聚合与补充 owner。"""

    @staticmethod
    def _merge_tmdb_auxiliary(
        mediainfo: MediaInfo,
        tmdb_media: MediaInfo,
    ) -> MediaInfo:
        """
        将 TMDB 兼容字段合并到主识别结果，不改变主数据源身份和展示信息。

        :param mediainfo: 主识别源返回的媒体信息
        :param tmdb_media: TMDB 辅助识别结果
        :return: 已补充 TMDB 兼容字段的主媒体信息
        """
        if not tmdb_media or tmdb_media.media_source != MediaSource.TMDB or not tmdb_media.tmdb_id:
            return mediainfo

        mediainfo.tmdb_id = tmdb_media.tmdb_id
        mediainfo.tmdb_info = tmdb_media.tmdb_info or mediainfo.tmdb_info
        if not mediainfo.genre_ids:
            mediainfo.genre_ids = list(tmdb_media.genre_ids or [])
        for field in ("imdb_id", "tvdb_id", "tvdb_slug", "collection_id"):
            if not getattr(mediainfo, field, None):
                setattr(mediainfo, field, getattr(tmdb_media, field, None))
        return mediainfo

    @staticmethod
    def _build_tmdb_supplement_meta(
        mediainfo: MediaInfo,
        metainfo: Optional[MetaBase] = None,
    ) -> MetaBase:
        """构造兼容旧调用方的 TMDB 附加识别参数。"""
        title = mediainfo.title or getattr(metainfo, "name", None) or ""
        tmdb_meta = MetaInfo(title)
        tmdb_meta.en_name = mediainfo.en_title or getattr(metainfo, "en_name", None)
        tmdb_meta.type = mediainfo.type or getattr(metainfo, "type", None) or MediaType.UNKNOWN
        season = mediainfo.season if mediainfo.season is not None else getattr(metainfo, "begin_season", None)
        tmdb_meta.begin_season = season
        season_year = None
        if season is not None and mediainfo.season_years:
            season_year = mediainfo.season_years.get(season) or mediainfo.season_years.get(str(season))
        tmdb_meta.year = season_year or mediainfo.year or getattr(metainfo, "year", None)
        return tmdb_meta

    @staticmethod
    def _media_alias_candidates(mediainfo: object) -> list[str]:
        """按稳定字段顺序提取单个来源可参与搜索匹配的标题候选。"""
        candidates = [
            getattr(mediainfo, field, None)
            for field in (
                "title",
                "original_title",
                "en_title",
                "hk_title",
                "tw_title",
                "sg_title",
            )
        ]
        candidates.extend(getattr(mediainfo, "names", None) or [])
        return [str(candidate).strip() for candidate in candidates if str(candidate or "").strip()]

    @classmethod
    def _merge_media_auxiliary(
        cls,
        mediainfo: MediaInfo,
        auxiliary_medias: Iterable[object],
        selected_sources: Optional[MediaSourceSelection],
    ) -> MediaInfo:
        """合并多来源别名，并仅接受 TMDB 的兼容字段补充。"""
        aliases = cls._media_alias_candidates(mediainfo)
        seen_aliases = {" ".join(alias.casefold().split()) for alias in aliases}
        selected = {selected_sources} if isinstance(selected_sources, MediaSource) else set(selected_sources or ())
        for auxiliary in auxiliary_medias or []:
            auxiliary_source = normalize_media_source(getattr(auxiliary, "media_source", None))
            if not auxiliary_source or (selected and auxiliary_source not in selected):
                continue
            for alias in cls._media_alias_candidates(auxiliary):
                normalized = " ".join(alias.casefold().split())
                if normalized in seen_aliases:
                    continue
                aliases.append(alias)
                seen_aliases.add(normalized)
            if auxiliary_source == MediaSource.TMDB and isinstance(auxiliary, MediaInfo):
                cls._merge_tmdb_auxiliary(mediainfo, auxiliary)
        mediainfo.names = aliases
        return mediainfo

    @staticmethod
    def _resolve_auxiliary_sources(
        media_source: Optional[MediaSourceSelection],
    ) -> Optional[MediaSourceSelection]:
        """解析请求级来源，未指定时使用用户配置的影视搜索来源集合。"""
        if media_source:
            return media_source
        configured = get_chain_runtime_config_snapshot().search_source
        try:
            return parse_media_source_selection(configured) or None
        except ValueError as err:
            logger.warning(f"媒体附加信息来源配置无效，跳过补充：{err}")
            return ()

    def supplement_media_info(
        self,
        mediainfo: Optional[Union[MediaInfo, MusicInfo]],
        media_source: Optional[MediaSourceSelection] = None,
        metainfo: Optional[MetaBase] = None,
    ) -> Optional[Union[MediaInfo, MusicInfo]]:
        """按用户启用的数据源聚合影视别名和受控的 TMDB 附加字段。"""
        if not mediainfo or isinstance(mediainfo, MusicInfo) or mediainfo.type == MediaType.MUSIC:
            return mediainfo
        selected_sources = self._resolve_auxiliary_sources(media_source)
        try:
            auxiliary_medias = (
                self.run_module(
                    "get_media_auxiliary_info",
                    mediainfo=mediainfo,
                    media_source=selected_sources,
                    metainfo=metainfo,
                )
                or []
            )
        except Exception as err:
            logger.warning(f"{mediainfo.title_year} 获取媒体附加信息失败：{err}")
            return mediainfo
        supplemented = self._merge_media_auxiliary(
            mediainfo,
            auxiliary_medias,
            selected_sources,
        )
        return self._finalize_recognition_result(
            supplemented,
            refresh=True,
        )

    async def async_supplement_media_info(
        self,
        mediainfo: Optional[Union[MediaInfo, MusicInfo]],
        media_source: Optional[MediaSourceSelection] = None,
        metainfo: Optional[MetaBase] = None,
    ) -> Optional[Union[MediaInfo, MusicInfo]]:
        """异步按用户启用的数据源聚合影视别名和受控 TMDB 字段。"""
        if not mediainfo or isinstance(mediainfo, MusicInfo) or mediainfo.type == MediaType.MUSIC:
            return mediainfo
        selected_sources = self._resolve_auxiliary_sources(media_source)
        try:
            auxiliary_medias = (
                await self.async_run_module(
                    "async_get_media_auxiliary_info",
                    mediainfo=mediainfo,
                    media_source=selected_sources,
                    metainfo=metainfo,
                )
                or []
            )
        except Exception as err:
            logger.warning(f"{mediainfo.title_year} 异步获取媒体附加信息失败：{err}")
            return mediainfo
        supplemented = self._merge_media_auxiliary(
            mediainfo,
            auxiliary_medias,
            selected_sources,
        )
        return await self._async_finalize_recognition_result(
            supplemented,
            refresh=True,
        )

    def supplement_tmdb_info(
        self,
        mediainfo: Optional[Union[MediaInfo, MusicInfo]],
        metainfo: Optional[MetaBase] = None,
    ) -> Optional[Union[MediaInfo, MusicInfo]]:
        """
        为任意主识别源补充 TMDB 辅助信息，同时保留原始媒体身份。

        :param mediainfo: 主识别源返回的媒体信息
        :param metainfo: 原始标题解析信息
        :return: 已补充 TMDB 辅助字段的原媒体对象
        """
        return self.supplement_media_info(
            mediainfo=mediainfo,
            media_source=MediaSource.TMDB,
            metainfo=metainfo,
        )
