import time
from collections.abc import Mapping
from typing import List, Optional, cast

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.db.base import DbOper
from app.db.models.subscribehistory import SubscribeHistory
from app.db.oper.query import (
    descending,
    enum_values,
    execute_page,
    media_identity_conditions,
    music_type_condition,
)
from app.schemas.common import JsonData
from app.schemas.query import (
    QueryPageRequest,
    QuerySortField,
    SubscriptionHistoryFilter,
)
from app.schemas.types import MediaSource


class SubscribeHistoryOper(DbOper):
    """
    订阅历史管理。
    """

    @staticmethod
    def _persistable(payload: Mapping[str, JsonData]) -> dict[str, JsonData]:
        """过滤历史表列并归一化整型开关和完成时间。"""
        values = {key: value for key, value in payload.items() if hasattr(SubscribeHistory, key) and key != "id"}
        for field in (
            "best_version",
            "best_version_full",
            "search_imdbid",
        ):
            value = values.get(field)
            if isinstance(value, bool):
                values[field] = int(value)
        values["date"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        return values

    def add(self, payload: Mapping[str, JsonData]) -> None:
        """兼容独立事务入口，新增一条订阅历史。"""
        self._stage_create(SubscribeHistory(**self._persistable(payload)))

    def stage_add(self, payload: Mapping[str, JsonData]) -> None:
        """在调用方同步 Session 中暂存订阅历史并 flush。"""
        if not isinstance(self._db, Session):
            raise RuntimeError("订阅历史暂存需要调用方提供同步 Session")
        self._db.add(SubscribeHistory(**self._persistable(payload)))
        self._db.flush()

    def exists(
        self,
        media_source: MediaSource,
        media_id: str,
        season: Optional[int] = None,
        episode_group: Optional[str] = None,
        music_type: Optional[str] = None,
    ) -> bool:
        """同步判断完整媒体身份是否已有订阅历史。"""
        return bool(
            self._execute_sync_query(
                lambda session: SubscribeHistory.exists(
                    session,
                    media_source=media_source,
                    media_id=media_id,
                    season=season,
                    episode_group=episode_group,
                    music_type=music_type,
                )
            )
        )

    def get_by_id(self, record_id: int) -> Optional[SubscribeHistory]:
        """按稳定记录 ID 读取单条订阅历史。"""
        return cast(
            Optional[SubscribeHistory],
            self._execute_sync_query(
                lambda session: (
                    session.execute(select(SubscribeHistory).where(SubscribeHistory.id == record_id)).scalars().first()
                )
            ),
        )

    def query(
        self,
        filters: SubscriptionHistoryFilter,
        page: QueryPageRequest,
    ) -> tuple[list[SubscribeHistory], int]:
        """按稳定筛选和分页合同读取订阅历史记录及总数。"""

        def execute(session: Session) -> tuple[list[SubscribeHistory], int]:
            """在同一会话中构造并执行订阅历史 count/page 查询。"""
            conditions = media_identity_conditions(SubscribeHistory, filters)
            ids = enum_values(filters.ids)
            names = enum_values(filters.names)
            usernames = enum_values(filters.usernames)
            media_types = enum_values(filters.media_types)
            if ids:
                conditions.append(SubscribeHistory.id.in_(ids))
            if names:
                conditions.append(SubscribeHistory.name.in_(names))
            if usernames:
                conditions.append(SubscribeHistory.username.in_(usernames))
            if media_types:
                conditions.append(SubscribeHistory.type.in_(media_types))
            if filters.season is not None:
                conditions.append(SubscribeHistory.season == filters.season)
            if filters.episode_group is not None:
                conditions.append(SubscribeHistory.episode_group == filters.episode_group)
            music_condition = music_type_condition(
                SubscribeHistory.music_type,
                filters.music_type,
            )
            if music_condition is not None:
                conditions.append(music_condition)

            count_statement = select(func.count(SubscribeHistory.id))
            page_statement = select(SubscribeHistory)
            if conditions:
                count_statement = count_statement.where(*conditions)
                page_statement = page_statement.where(*conditions)
            descending_order = descending(page)
            if page.sort.field == QuerySortField.ID:
                primary = SubscribeHistory.id.desc() if descending_order else SubscribeHistory.id.asc()
                secondary = SubscribeHistory.date.desc() if descending_order else SubscribeHistory.date.asc()
            else:
                primary = (
                    SubscribeHistory.date.desc().nullslast()
                    if descending_order
                    else SubscribeHistory.date.asc().nullsfirst()
                )
                secondary = SubscribeHistory.id.desc() if descending_order else SubscribeHistory.id.asc()
            page_statement = page_statement.order_by(primary, secondary)
            return cast(
                tuple[list[SubscribeHistory], int],
                execute_page(session, count_statement, page_statement, page),
            )

        return self._execute_sync_query(execute)

    async def async_list_by_type(
        self,
        mtype: str,
        page: int = 1,
        count: int = 30,
    ) -> List[SubscribeHistory]:
        """
        异步按媒体类型分页查询订阅历史。
        """
        return await self._execute_async_query(
            lambda session: SubscribeHistory.async_list_by_type(
                session,
                mtype=mtype,
                page=page,
                count=count,
            )
        )

    async def async_list_by_type_and_username(
        self,
        mtype: str,
        username: str,
        page: int = 1,
        count: int = 30,
    ) -> List[SubscribeHistory]:
        """异步按媒体类型和用户分页查询订阅历史。"""
        return await self._execute_async_query(
            lambda session: SubscribeHistory.async_list_by_type_and_username(
                session,
                mtype=mtype,
                username=username,
                page=page,
                count=count,
            )
        )

    async def async_count_by_type(self, mtype: str) -> int:
        """异步统计指定媒体类型的订阅历史。"""
        return await self._execute_async_query(
            lambda session: SubscribeHistory.async_count_by_type(session, mtype)
        )

    async def async_count_by_type_and_username(
        self,
        mtype: str,
        username: str,
    ) -> int:
        """异步统计指定媒体类型和 owner 的订阅历史。"""
        return await self._execute_async_query(
            lambda session: SubscribeHistory.async_count_by_type_and_username(
                session,
                mtype,
                username,
            )
        )

    async def async_get(self, history_id: int) -> Optional[SubscribeHistory]:
        """异步按 ID 查询订阅历史。"""

        async def query(session: AsyncSession) -> Optional[SubscribeHistory]:
            """在调用方异步会话中按主键查询历史。"""
            result = await session.execute(select(SubscribeHistory).where(SubscribeHistory.id == history_id))
            return cast(Optional[SubscribeHistory], result.scalars().first())

        return cast(
            Optional[SubscribeHistory],
            await self._execute_async_query(query),
        )

    async def async_delete(self, history_id: int) -> None:
        """异步删除订阅历史。"""
        await self._stage_async_delete(SubscribeHistory, history_id)
