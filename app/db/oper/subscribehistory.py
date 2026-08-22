from typing import List, Optional

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
        return await SubscribeHistory.async_list_by_type(
            self._db,
            mtype=mtype,
            page=page,
            count=count,
        )

    async def async_list_by_type_and_username(
        self,
        mtype: str,
        username: str,
        page: int = 1,
        count: int = 30,
    ) -> List[SubscribeHistory]:
        """异步按媒体类型和用户分页查询订阅历史。"""
        return await SubscribeHistory.async_list_by_type_and_username(
            self._db,
            mtype=mtype,
            username=username,
            page=page,
            count=count,
        )

    async def async_get(self, history_id: int) -> Optional[SubscribeHistory]:
        """异步按 ID 查询订阅历史。"""
        return await SubscribeHistory.async_get(self._db, history_id)

    async def async_delete(self, history_id: int) -> None:
        """异步删除订阅历史。"""
        await self._stage_async_delete(SubscribeHistory, history_id)
