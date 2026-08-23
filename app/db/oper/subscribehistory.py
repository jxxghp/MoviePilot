from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import DbOper
from app.db.models.subscribehistory import SubscribeHistory


class SubscribeHistoryOper(DbOper):
    """
    订阅历史管理。
    """

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

    async def async_get(self, history_id: int) -> Optional[SubscribeHistory]:
        """异步按 ID 查询订阅历史。"""
        async def query(session: AsyncSession) -> Optional[SubscribeHistory]:
            """在调用方异步会话中按主键查询历史。"""
            result = await session.execute(
                select(SubscribeHistory).where(SubscribeHistory.id == history_id)
            )
            return result.scalars().first()

        return await self._execute_async_query(query)

    async def async_delete(self, history_id: int) -> None:
        """异步删除订阅历史。"""
        await self._stage_async_delete(SubscribeHistory, history_id)
