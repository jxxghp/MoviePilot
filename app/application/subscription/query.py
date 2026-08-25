"""订阅存在性、来源定位和类型状态查询应用服务。"""

from __future__ import annotations

from typing import Any, Optional, Protocol

from app.domain.context import MediaInfo
from app.domain.meta.metabase import MetaBase
from app.schemas.media import resolve_media_identity
from app.schemas.types import MediaSource, MediaType
from app.schemas.workflow import Subscribe as SubscribeView


class SubscriptionQueryRepository(Protocol):
    """描述订阅查询切片所需的最小仓储能力。"""

    def exists(self, **identity: Any) -> bool:
        """按完整订阅身份判断记录是否存在。"""
        ...

    def get_by(self, **identity: Any) -> Optional[Any]:
        """按来源关键字中的订阅身份读取单条记录。"""
        ...

    def list(self, state: Optional[str] = None) -> list[Any]:
        """按可选状态集合读取订阅记录。"""
        ...


class AsyncSubscriptionQueryRepository(Protocol):
    """公开订阅查询所需的异步持久化端口。"""

    async def async_list(self) -> list[Any]:
        """读取全部订阅。"""
        ...

    async def async_list_by_username(self, username: str) -> list[Any]:
        """读取指定用户订阅。"""
        ...

    async def async_get(self, subscribe_id: int) -> Optional[Any]:
        """按 ID 读取订阅。"""
        ...

    async def async_list_by_media_identity(
        self,
        media_source: Any,
        media_id: str,
        music_type: Optional[str] = None,
    ) -> list[Any]:
        """按规范媒体身份读取订阅。"""
        ...


class AsyncSubscriptionHistoryQueryRepository(Protocol):
    """订阅历史公开查询所需的异步持久化端口。"""

    async def async_list_by_type(
        self,
        mtype: str,
        page: int = 1,
        count: int = 30,
    ) -> list[Any]:
        """按媒体类型分页读取订阅历史。"""
        ...

    async def async_list_by_type_and_username(
        self,
        mtype: str,
        username: str,
        page: int = 1,
        count: int = 30,
    ) -> list[Any]:
        """按媒体类型和用户分页读取订阅历史。"""
        ...


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
        repository: SubscriptionQueryRepository,
        *,
        async_repository: Optional[AsyncSubscriptionQueryRepository] = None,
        history_repository: Optional[AsyncSubscriptionHistoryQueryRepository] = None,
    ) -> None:
        """保存订阅查询仓储端口。"""
        self._repository = repository
        self._async_repository = async_repository
        self._history_repository = history_repository

    async def list_public(
        self,
        username: Optional[str] = None,
    ) -> list[SubscribeView]:
        """读取公开订阅列表并转换为稳定 DTO。"""
        if self._async_repository is None:
            raise RuntimeError("异步订阅查询端口未注册")
        if username:
            records = await self._async_repository.async_list_by_username(
                username=username
            )
        else:
            records = await self._async_repository.async_list()
        return [SubscribeView.model_validate(record) for record in records]

    async def get_public(self, subscribe_id: int) -> Optional[SubscribeView]:
        """按 ID 读取订阅 DTO。"""
        if self._async_repository is None:
            raise RuntimeError("异步订阅查询端口未注册")
        record = await self._async_repository.async_get(subscribe_id)
        return SubscribeView.model_validate(record) if record else None

    async def list_by_media_identity(
        self,
        media_source: Any,
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
            SubscribeView.model_validate(record)
            for record in records
            if self._matches_music_type(record, music_type)
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

    @staticmethod
    def _matches_music_type(record: Any, music_type: Optional[str]) -> bool:
        """把迁移前未标注音乐类型的记录兼容为单曲。"""
        if not music_type:
            return True
        value = getattr(record, "music_type", None)
        return value == music_type or (
            music_type == "recording" and value is None
        )

    def exists(
        self,
        mediainfo: MediaInfo,
        meta: Optional[MetaBase] = None,
    ) -> bool:
        """按媒体身份、季、剧集组和音乐实体类型判断订阅是否存在。"""
        media_source, media_id = resolve_media_identity(media=mediainfo)
        return bool(self._repository.exists(
            media_source=media_source,
            media_id=media_id,
            music_type=getattr(mediainfo, "music_type", None)
            if mediainfo.type == MediaType.MUSIC else None,
            season=meta.begin_season if meta else None,
            episode_group=mediainfo.episode_group,
        ))

    @classmethod
    def _has_media_identity(cls, identity: dict[str, Any]) -> bool:
        """判断来源关键字是否已带成对的媒体身份。"""
        media_id = identity.get("media_id")
        return bool(identity.get("media_source")) and media_id not in (
            None,
            "",
            0,
            "0",
        )

    @classmethod
    def _legacy_media_identity(cls, source_keyword: dict[str, Any]) -> dict[str, Any]:
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

    def get_by_source(self, source_keyword: Optional[dict]) -> Optional[Any]:
        """从已解析来源关键字筛出稳定身份字段并读取订阅。"""
        if not source_keyword:
            return None
        identity = {
            key: value
            for key, value in source_keyword.items()
            if key in self._SOURCE_FIELDS
        }
        if not self._has_media_identity(identity):
            identity.update(self._legacy_media_identity(source_keyword))
        if not identity.get("type") or not self._has_media_identity(identity):
            return None
        return self._repository.get_by(**identity)

    def has_music(self, searchable_states: str) -> bool:
        """判断给定可搜索状态内是否至少存在一个音乐订阅。"""
        return any(
            subscribe.type == MediaType.MUSIC.value
            for subscribe in self._repository.list(searchable_states) or []
        )
