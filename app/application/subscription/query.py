"""订阅存在性、来源定位和类型状态查询应用服务。"""

from __future__ import annotations

from typing import Any, Optional, Protocol

from app.domain.context import MediaInfo
from app.domain.meta.metabase import MetaBase
from app.schemas.media import resolve_media_identity
from app.schemas.types import MediaType


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


class SubscriptionQueryService:
    """封装不修改订阅状态的三个公开查询用例。"""

    _SOURCE_FIELDS = {
        "type",
        "season",
        "media_source",
        "media_id",
        "music_type",
    }

    def __init__(self, repository: SubscriptionQueryRepository) -> None:
        """保存订阅查询仓储端口。"""
        self._repository = repository

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

    def get_by_source(self, source_keyword: Optional[dict]) -> Optional[Any]:
        """从已解析来源关键字筛出稳定身份字段并读取订阅。"""
        if not source_keyword:
            return None
        identity = {
            key: value
            for key, value in source_keyword.items()
            if key in self._SOURCE_FIELDS
        }
        return self._repository.get_by(**identity)

    def has_music(self, searchable_states: str) -> bool:
        """判断给定可搜索状态内是否至少存在一个音乐订阅。"""
        return any(
            subscribe.type == MediaType.MUSIC.value
            for subscribe in self._repository.list(searchable_states) or []
        )
