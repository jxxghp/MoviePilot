import time

from app.db import DbOper
from app.db.models.subscribehistory import SubscribeHistory


class SubscribeHistoryOper(DbOper):
    """
    订阅历史管理
    """

    def add(self, **kwargs):
        """
        新增订阅
        """
        # 去除kwargs中 SubscribeHistory 没有的字段
        kwargs = {k: v for k, v in kwargs.items() if hasattr(SubscribeHistory, k)}
        # 更新完成订阅时间
        kwargs.update({"date": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())})
        # 去掉主键
        if "id" in kwargs:
            kwargs.pop("id")
        subscribe = SubscribeHistory(**kwargs)
        subscribe.create(self._db)

    def list_by_type(self, mtype: str, page: int = 1, count: int = 30) -> SubscribeHistory:
        """
        获取指定类型的订阅
        """
        return SubscribeHistory.list_by_type(self._db, mtype=mtype, page=page, count=count)
