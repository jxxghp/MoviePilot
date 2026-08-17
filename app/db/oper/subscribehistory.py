from typing import List

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
