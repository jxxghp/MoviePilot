"""Servarr 兼容接口使用的订阅投影和数据用例。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Protocol

from app.schemas.types import MediaSource


@dataclass(frozen=True, slots=True)
class ServarrSubscription:
    """隔离 Servarr 端点与订阅 ORM 模型的稳定投影。"""

    id: int
    name: Optional[str]
    year: Optional[str]
    type: Optional[str]
    season: Optional[int]
    poster: Optional[str]
    media_source: Optional[str]
    media_id: Optional[str]


class ServarrAsyncSubscriptionRepository(Protocol):
    """Servarr 异步订阅用例需要的最小仓储端口。"""

    async def async_list(self) -> list[Any]:
        """读取全部订阅。"""
        ...

    async def async_get(self, subscribe_id: int) -> Optional[Any]:
        """按主键读取订阅。"""
        ...

    async def async_list_by_media_identity(
        self,
        media_source: MediaSource,
        media_id: str,
        music_type: Optional[str] = None,
    ) -> list[Any]:
        """按媒体身份读取订阅。"""
        ...

    async def async_exists(
        self,
        media_source: MediaSource,
        media_id: str,
        season: Optional[int] = None,
        episode_group: Optional[str] = None,
        music_type: Optional[str] = None,
    ) -> Optional[Any]:
        """按媒体身份读取命中的订阅。"""
        ...

    async def async_delete(self, subscribe_id: int) -> None:
        """按主键删除订阅。"""
        ...


class ServarrSyncSubscriptionRepository(Protocol):
    """Servarr 同步 lookup 用例需要的最小仓储端口。"""

    def list_by_media_identity(
        self,
        media_source: MediaSource,
        media_id: str,
        music_type: Optional[str] = None,
    ) -> list[Any]:
        """按媒体身份读取订阅。"""
        ...


class ServarrSubscriptionService:
    """提供 Servarr 路由所需的订阅查询、查重和删除能力。"""

    def __init__(
        self,
        *,
        async_repository: ServarrAsyncSubscriptionRepository,
        sync_repository: ServarrSyncSubscriptionRepository,
    ) -> None:
        """保存请求级同步和异步订阅仓储。"""
        self._async_repository = async_repository
        self._sync_repository = sync_repository

    async def list(self) -> list[ServarrSubscription]:
        """读取全部订阅并转换为脱离 ORM 会话的投影。"""
        return [self._project(record) for record in await self._async_repository.async_list()]

    async def get(self, subscribe_id: int) -> Optional[ServarrSubscription]:
        """按主键读取订阅投影。"""
        record = await self._async_repository.async_get(subscribe_id)
        return self._project(record) if record else None

    async def list_by_media_identity(
        self,
        media_source: MediaSource,
        media_id: str,
    ) -> list[ServarrSubscription]:
        """异步按媒体身份读取订阅投影。"""
        records = await self._async_repository.async_list_by_media_identity(
            media_source=media_source,
            media_id=media_id,
        )
        return [self._project(record) for record in records]

    def list_by_media_identity_sync(
        self,
        media_source: MediaSource,
        media_id: str,
    ) -> list[ServarrSubscription]:
        """同步按媒体身份读取订阅投影。"""
        records = self._sync_repository.list_by_media_identity(
            media_source=media_source,
            media_id=media_id,
        )
        return [self._project(record) for record in records]

    async def exists(
        self,
        *,
        media_source: MediaSource,
        media_id: str,
        season: Optional[int] = None,
    ) -> bool:
        """判断指定媒体身份和季是否已有订阅。"""
        record = await self._async_repository.async_exists(
            media_source=media_source,
            media_id=media_id,
            season=season,
        )
        return record is not None

    async def delete(self, subscribe_id: int) -> bool:
        """删除存在的订阅并报告是否实际命中。"""
        if not await self._async_repository.async_get(subscribe_id):
            return False
        await self._async_repository.async_delete(subscribe_id)
        return True

    @staticmethod
    def _project(record: Any) -> ServarrSubscription:
        """从数据库记录复制 Servarr 路由所需的最小字段。"""
        return ServarrSubscription(
            id=record.id,
            name=getattr(record, "name", None),
            year=getattr(record, "year", None),
            type=getattr(record, "type", None),
            season=getattr(record, "season", None),
            poster=getattr(record, "poster", None),
            media_source=getattr(record, "media_source", None),
            media_id=getattr(record, "media_id", None),
        )
