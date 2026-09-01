"""订阅存在性、来源定位和类型状态查询应用服务。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Optional

from app.application.subscription.contract import (
    SubscriptionHistoryQueryPort,
    SubscriptionIdentity,
    SubscriptionQueryPort,
    SubscriptionSnapshot,
)
from app.domain.context import MediaInfo
from app.domain.meta.metabase import MetaBase
from app.schemas.common import JsonData
from app.schemas.media import resolve_media_identity
from app.schemas.types import MediaSource, MediaType
from app.schemas.workflow import Subscribe as SubscribeView


class SubscriptionQueryService:
    """封装不修改订阅状态的三个公开查询用例。"""

    _SOURCE_FIELDS = {
        "type",
        "season",
        "media_source",
        "media_id",
        "music_type",
    }
    _LEGACY_ID_FIELDS: tuple[tuple[str, MediaSource], ...] = (
        ("tmdbid", MediaSource.TMDB),
        ("doubanid", MediaSource.Douban),
        ("bangumiid", MediaSource.Bangumi),
        ("anilistid", MediaSource.AniList),
        ("imdbid", MediaSource.IMDb),
        ("tvdbid", MediaSource.TVDB),
    )

    def __init__(
        self,
        repository: SubscriptionQueryPort,
        *,
        async_repository: Optional[SubscriptionQueryPort] = None,
        history_repository: Optional[SubscriptionHistoryQueryPort] = None,
    ) -> None:
        """保存订阅查询仓储端口。"""
        self._repository = repository
        self._async_repository = async_repository
        self._history_repository = history_repository

    async def list_public(
        self,
        username: Optional[str] = None,
        page: Optional[int] = None,
        count: Optional[int] = None,
    ) -> list[SubscribeView]:
        """按 owner 和可选数据库窗口读取公开订阅 DTO。"""
        if self._async_repository is None:
            raise RuntimeError("异步订阅查询端口未注册")
        if username:
            if page is None and count is None:
                records = await self._async_repository.async_list_by_username(
                    username=username
                )
            else:
                records = await self._async_repository.async_list_by_username(
                    username=username,
                    page=page,
                    count=count,
                )
        else:
            if page is None and count is None:
                records = await self._async_repository.async_list()
            else:
                records = await self._async_repository.async_list(
                    page=page,
                    count=count,
                )
        return [SubscribeView.model_validate(record) for record in records]

    async def count_public(self, username: Optional[str] = None) -> int:
        """按 owner 范围返回公开订阅精确总数。"""
        if self._async_repository is None:
            raise RuntimeError("异步订阅查询端口未注册")
        return await self._async_repository.async_count(username=username)

    async def get_public(self, subscribe_id: int) -> Optional[SubscribeView]:
        """按 ID 读取订阅 DTO。"""
        if self._async_repository is None:
            raise RuntimeError("异步订阅查询端口未注册")
        record = await self._async_repository.async_get(subscribe_id)
        return SubscribeView.model_validate(record) if record else None

    async def list_by_media_identity(
        self,
        media_source: MediaSource,
        media_id: str,
        music_type: Optional[str] = None,
    ) -> list[SubscribeView]:
        """按媒体身份读取订阅 DTO，并兼容旧音乐记录。"""
        if self._async_repository is None:
            raise RuntimeError("异步订阅查询端口未注册")
        records = await self._async_repository.async_list_by_media_identity(
            media_source=media_source,
            media_id=media_id,
            music_type=music_type,
        )
        return [
            SubscribeView.model_validate(record) for record in records if self._matches_music_type(record, music_type)
        ]

    async def list_history(
        self,
        mtype: str,
        *,
        page: int = 1,
        count: int = 30,
        username: Optional[str] = None,
    ) -> list[SubscribeView]:
        """分页读取订阅历史 DTO。"""
        if self._history_repository is None:
            raise RuntimeError("订阅历史查询端口未注册")
        if username:
            records = await self._history_repository.async_list_by_type_and_username(
                mtype,
                username,
                page,
                count,
            )
        else:
            records = await self._history_repository.async_list_by_type(
                mtype,
                page,
                count,
            )
        result = []
        for record in records:
            item = SubscribeView.model_validate(record)
            if item.type == MediaType.TV.value:
                item.total_episode = 0
                item.lack_episode = 0
            result.append(item)
        return result

    async def count_history(
        self,
        mtype: str,
        *,
        username: Optional[str] = None,
    ) -> int:
        """按与历史列表相同的 owner 范围返回精确总数。"""
        if self._history_repository is None:
            raise RuntimeError("订阅历史查询端口未注册")
        if username:
            return await self._history_repository.async_count_by_type_and_username(
                mtype,
                username,
            )
        return await self._history_repository.async_count_by_type(mtype)

    @staticmethod
    def _matches_music_type(
        record: SubscriptionSnapshot,
        music_type: Optional[str],
    ) -> bool:
        """把迁移前未标注音乐类型的记录兼容为单曲。"""
        if not music_type:
            return True
        value = record.music_type
        return value == music_type or (music_type == "recording" and value is None)

    def exists(
        self,
        mediainfo: MediaInfo,
        meta: Optional[MetaBase] = None,
    ) -> bool:
        """按媒体身份、季、剧集组和音乐实体类型判断订阅是否存在。"""
        media_source, media_id = resolve_media_identity(media=mediainfo)
        if media_source is None or media_id is None:
            return False
        return self._repository.exists(
            SubscriptionIdentity(
                media_source=media_source,
                media_id=str(media_id),
                music_type=getattr(mediainfo, "music_type", None) if mediainfo.type == MediaType.MUSIC else None,
                season=meta.begin_season if meta else None,
                episode_group=mediainfo.episode_group,
            )
        )

    @classmethod
    def _has_media_identity(cls, identity: Mapping[str, JsonData]) -> bool:
        """判断来源关键字是否已带成对的媒体身份。"""
        media_id = identity.get("media_id")
        return bool(identity.get("media_source")) and media_id not in (
            None,
            "",
            0,
            "0",
        )

    @classmethod
    def _legacy_media_identity(
        cls,
        source_keyword: Mapping[str, JsonData],
    ) -> dict[str, JsonData]:
        """把 v2 订阅来源里的 tmdbid/doubanid 等补成 media_source + media_id。"""
        for field, source in cls._LEGACY_ID_FIELDS:
            raw = source_keyword.get(field)
            if raw in (None, "", 0, "0"):
                continue
            return {
                "media_source": source,
                "media_id": str(raw),
            }
        return {}

    def get_by_source(
        self,
        source_keyword: Optional[Mapping[str, JsonData]],
    ) -> Optional[SubscriptionSnapshot]:
        """从已解析来源关键字筛出稳定身份字段并读取订阅。"""
        if not source_keyword:
            return None
        identity = {key: value for key, value in source_keyword.items() if key in self._SOURCE_FIELDS}
        if not self._has_media_identity(identity):
            identity.update(self._legacy_media_identity(source_keyword))
        if not identity.get("type") or not self._has_media_identity(identity):
            return None
        raw_source = identity.get("media_source")
        raw_id = identity.get("media_id")
        raw_type = identity.get("type")
        if not isinstance(raw_source, (str, MediaSource)):
            return None
        if not isinstance(raw_id, (str, int)) or not isinstance(raw_type, str):
            return None
        raw_season = identity.get("season")
        raw_music_type = identity.get("music_type")
        return self._repository.get_by(
            SubscriptionIdentity(
                media_source=MediaSource(raw_source),
                media_id=str(raw_id),
                type=raw_type,
                season=raw_season if isinstance(raw_season, int) else None,
                music_type=raw_music_type if isinstance(raw_music_type, str) else None,
            )
        )

    def has_music(self, searchable_states: str) -> bool:
        """判断给定可搜索状态内是否至少存在一个音乐订阅。"""
        return any(
            subscribe.type == MediaType.MUSIC.value for subscribe in self._repository.list(searchable_states) or []
        )
