from typing import Any

from app.db import DbOper
from app.db.models.transferhistory import TransferHistory


class TransferHistoryOper(DbOper):
    """
    转移历史管理
    """

    def get_by_title(self, title: str) -> Any:
        """
        按标题查询转移记录
        :param title: 数据key
        """
        return TransferHistory.search_by_title(self._db, title)

    def add(self, **kwargs):
        """
        新增转移历史
        """
        transferhistory = TransferHistory(**kwargs)
        return transferhistory.create(self._db)
