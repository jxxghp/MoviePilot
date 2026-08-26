"""影视识别模块共享的附加信息能力实现。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Optional

from app.domain.context import MediaInfo
from app.domain.media import is_media_source_enabled
from app.domain.meta.metabase import MetaBase
from app.domain.metainfo import MetaInfo
from app.schemas.types import MediaSource, MediaSourceSelection, MediaType


class MediaAuxiliaryProviderMixin:
    """把现有识别接口适配为可聚合的影视附加信息能力。"""

    auxiliary_media_source: MediaSource

    @staticmethod
    def _build_auxiliary_meta(
            mediainfo: MediaInfo,
            metainfo: Optional[MetaBase] = None,
    ) -> MetaBase:
        """根据主识别结果构造不携带原来源身份的标题查询参数。"""
        # MetaInfo 是工厂函数，实际返回 MetaBase 及其子类实例
        meta = MetaInfo(mediainfo.title or getattr(metainfo, "name", None) or "")
        if not meta.en_name:
            meta.en_name = mediainfo.en_title or getattr(metainfo, "en_name", None)
        meta.type = mediainfo.type or getattr(metainfo, "type", None) or MediaType.UNKNOWN
        meta.begin_season = (
            mediainfo.season
            if mediainfo.season is not None
            else getattr(metainfo, "begin_season", None)
        )
        season_year = None
        if meta.begin_season is not None and mediainfo.season_years:
            season_year = (
                mediainfo.season_years.get(meta.begin_season)
                or mediainfo.season_years.get(str(meta.begin_season))
            )
        meta.year = season_year or mediainfo.year or getattr(metainfo, "year", None)
        return meta

    def _auxiliary_recognize_kwargs(
            self,
            mediainfo: MediaInfo,
            metainfo: Optional[MetaBase] = None,
    ) -> dict[str, object]:
        """构造当前来源的识别参数，同源媒体优先使用来源原生 ID。"""
        same_source = mediainfo.media_source == self.auxiliary_media_source
        return {
            "meta": self._build_auxiliary_meta(mediainfo, metainfo),
            "mtype": mediainfo.type,
            "media_source": self.auxiliary_media_source,
            "media_id": mediainfo.media_id if same_source else None,
            "episode_group": mediainfo.episode_group,
            "cache": True,
        }

    def get_media_auxiliary_info(
            self,
            mediainfo: MediaInfo,
            media_source: Optional[MediaSourceSelection] = None,
            metainfo: Optional[MetaBase] = None,
    ) -> list[MediaInfo]:
        """使用当前来源同步获取影视别名等附加信息。"""
        if (
                not mediainfo
                or mediainfo.type not in (MediaType.MOVIE, MediaType.TV)
                or not is_media_source_enabled(media_source, self.auxiliary_media_source)
        ):
            return []
        recognize: Callable[..., Optional[MediaInfo]] = getattr(self, "recognize_media")
        result = recognize(**self._auxiliary_recognize_kwargs(mediainfo, metainfo))
        return [result] if result else []

    async def async_get_media_auxiliary_info(
            self,
            mediainfo: MediaInfo,
            media_source: Optional[MediaSourceSelection] = None,
            metainfo: Optional[MetaBase] = None,
    ) -> list[MediaInfo]:
        """使用当前来源异步获取影视别名等附加信息。"""
        if (
                not mediainfo
                or mediainfo.type not in (MediaType.MOVIE, MediaType.TV)
                or not is_media_source_enabled(media_source, self.auxiliary_media_source)
        ):
            return []
        recognize: Callable[..., Awaitable[Optional[MediaInfo]]] = getattr(
            self,
            "async_recognize_media",
        )
        result = await recognize(**self._auxiliary_recognize_kwargs(mediainfo, metainfo))
        return [result] if result else []
