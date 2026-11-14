"""
Servarr 兼容接口使用的订阅投影和数据用例。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, List, Optional, Protocol

from app.schemas.types import MediaSource, MediaType

if TYPE_CHECKING:
    from app.application.subscription.delete import AsyncUnitOfWork


@dataclass(frozen=True, slots=True)
class ServarrSubscription:
    """
    隔离 Servarr 端点与订阅 ORM 模型的稳定投影。

    :param id: MoviePilot 订阅记录主键
    :param name: 订阅媒体名称，旧记录可能未填写
    :param year: 媒体发行年份字符串，未知时为 None
    :param type: MoviePilot 媒体类型名称，未知时为 None
    :param season: 电视剧订阅季号，电影或未指定季时为 None
    :param poster: 媒体海报地址，未知时为 None
    :param media_source: 统一媒体身份的数据来源，旧记录可为空
    :param media_id: 对应来源的原生媒体标识，旧记录可为空
    """

    id: int
    name: Optional[str]
    year: Optional[str]
    type: Optional[str]
    season: Optional[int]
    poster: Optional[str]
    media_source: Optional[str]
    media_id: Optional[str]


class ServarrAsyncSubscriptionRepository(Protocol):
    """
    Servarr 异步订阅用例需要的最小仓储端口。
    """

    async def async_list(self) -> list[Any]:
        """
        读取全部订阅。

        :return: 全部订阅记录列表，无记录时为空列表
        """
        ...

    async def async_get(self, subscribe_id: int) -> Optional[Any]:
        """
        按主键读取订阅。

        :param subscribe_id: MoviePilot 订阅主键
        :return: 匹配的订阅记录，不存在时为 None
        """
        ...

    async def async_list_by_media_identity(
        self,
        media_source: MediaSource,
        media_id: str,
        music_type: Optional[str] = None,
    ) -> list[Any]:
        """
        按媒体身份读取订阅。

        :param media_source: 统一媒体身份的数据来源
        :param media_id: 对应来源的原生媒体标识
        :param music_type: 音乐实体类型过滤；None 不限定类型，recording 兼容未标记类型的旧单曲记录
        :return: 匹配该媒体身份的订阅记录列表，无匹配时为空列表
        """
        ...

    async def async_exists(
        self,
        media_source: MediaSource,
        media_id: str,
        season: Optional[int] = None,
        episode_group: Optional[str] = None,
        music_type: Optional[str] = None,
        mtype: Optional[str] = None,
    ) -> Optional[Any]:
        """
        按媒体身份读取命中的订阅。

        :param media_source: 统一媒体身份的数据来源
        :param media_id: 对应来源的原生媒体标识
        :param season: 电视剧季号，None 表示不附加季号过滤
        :param episode_group: 剧集组标识，None 只匹配未设置剧集组的记录
        :param music_type: 音乐实体类型过滤；None 不限定类型，recording 兼容未标记类型的旧单曲记录
        :param mtype: 媒体类型，防止电影和电视剧的同编号冲突
        :return: 满足媒体身份及附加条件的订阅记录，不存在时为 None
        """
        ...

    async def async_delete(self, subscribe_id: int) -> None:
        """
        按主键删除订阅。

        :param subscribe_id: 待删除的 MoviePilot 订阅主键
        """
        ...


class ServarrSyncSubscriptionRepository(Protocol):
    """
    Servarr 同步 lookup 用例需要的最小仓储端口。
    """

    def list_by_media_identity(
        self,
        media_source: MediaSource,
        media_id: str,
        music_type: Optional[str] = None,
    ) -> list[Any]:
        """
        按媒体身份读取订阅。

        :param media_source: 统一媒体身份的数据来源
        :param media_id: 对应来源的原生媒体标识
        :param music_type: 音乐实体类型过滤；None 不限定类型，recording 兼容未标记类型的旧单曲记录
        :return: 匹配该媒体身份的订阅记录列表，无匹配时为空列表
        """
        ...


class ServarrSubscriptionService:
    """
    提供 Servarr 路由所需的订阅查询、查重和删除能力。
    """

    def __init__(
        self,
        *,
        async_repository: ServarrAsyncSubscriptionRepository,
        sync_repository: ServarrSyncSubscriptionRepository,
        unit_of_work: Optional[AsyncUnitOfWork] = None,
    ) -> None:
        """
        保存订阅仓储，并按需保存请求级异步事务端口。
        """
        self._async_repository = async_repository
        self._sync_repository = sync_repository
        self._unit_of_work = unit_of_work

    async def list(self) -> List[ServarrSubscription]:
        """
        读取全部订阅并转换为脱离 ORM 会话的投影。
        """
        return [self._project(record) for record in await self._async_repository.async_list()]

    async def get(self, subscribe_id: int) -> Optional[ServarrSubscription]:
        """
        按主键读取订阅投影。
        """
        record = await self._async_repository.async_get(subscribe_id)
        return self._project(record) if record else None

    async def list_by_media_identity(
        self,
        media_source: MediaSource,
        media_id: str,
    ) -> List[ServarrSubscription]:
        """
        异步按媒体身份读取订阅投影。
        """
        records = await self._async_repository.async_list_by_media_identity(
            media_source=media_source,
            media_id=media_id,
        )
        return [self._project(record) for record in records]

    def list_by_media_identity_sync(
        self,
        media_source: MediaSource,
        media_id: str,
    ) -> List[ServarrSubscription]:
        """
        同步按媒体身份读取订阅投影。
        """
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
        """
        判断指定媒体身份和季是否已有订阅。
        """
        return await self.get_existing(media_source=media_source, media_id=media_id, season=season) is not None

    async def get_existing(
        self, *, media_source: MediaSource, media_id: str, season: Optional[int] = None,
    ) -> Optional[ServarrSubscription]:
        """
        返回命中身份的实际订阅，供重复添加响应保留正确主键。

        :param media_source: 订阅数据来源
        :param media_id: 来源原生标识
        :param season: 电视剧季号；None 时不筛选季
        :return: 已有订阅的稳定投影，未命中为 None
        """
        record = await self._async_repository.async_exists(
            media_source=media_source,
            media_id=media_id,
            season=season,
            mtype=MediaType.TV.value if season is not None else MediaType.MOVIE.value,
        )
        return self._project(record) if record is not None else None

    async def delete(self, subscribe_id: int) -> bool:
        """
        删除存在的订阅并报告是否实际命中。
        """
        if not await self._async_repository.async_get(subscribe_id):
            return False
        try:
            await self._async_repository.async_delete(subscribe_id)
            if self._unit_of_work is not None:
                await self._unit_of_work.commit()
        except Exception:
            if self._unit_of_work is not None:
                await self._unit_of_work.rollback()
            raise
        return True

    @staticmethod
    def _project(record: Any) -> ServarrSubscription:
        """
        从数据库记录复制 Servarr 路由所需的最小字段。
        """
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
