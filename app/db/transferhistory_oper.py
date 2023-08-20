import time
from typing import Any

from app.db import DbOper
from app.db.models.transferhistory import TransferHistory


class TransferHistoryOper(DbOper):
    """
    转移历史管理
    """

    def get(self, historyid: int) -> Any:
        """
        获取转移历史
        :param historyid: 转移历史id
        """
        return TransferHistory.get(self._db, historyid)

    def get_by_title(self, title: str) -> Any:
        """
        按标题查询转移记录
        :param title: 数据key
        """
        return TransferHistory.list_by_title(self._db, title)

    def get_by_src(self, src: str) -> Any:
        """
        按源查询转移记录
        :param src: 数据key
        """
        return TransferHistory.get_by_src(self._db, src)

    def add(self, **kwargs):
        """
        新增转移历史
        """
        if kwargs.get("download_hash"):
            transferhistory = TransferHistory.get_by_hash(self._db, kwargs.get("download_hash"))
            if transferhistory:
                transferhistory.delete(self._db, transferhistory.id)
        kwargs.update({
            "date": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        })
        return TransferHistory(**kwargs).create(self._db)

    def statistic(self, days: int = 7):
        """
        统计最近days天的下载历史数量
        """
        return TransferHistory.statistic(self._db, days)

    def get_by(self, mtype: str = None, title: str = None, year: str = None,
               season: str = None, episode: str = None, tmdbid: str = None) -> Any:
        """
        按类型、标题、年份、季集查询转移记录
        """
        return TransferHistory.list_by(db=self._db,
                                       mtype=mtype,
                                       title=title,
                                       year=year,
                                       season=season,
                                       episode=episode,
                                       tmdbid=tmdbid)

    def delete(self, historyid):
        """
        删除转移记录
        """
        TransferHistory.delete(self._db, historyid)

    def truncate(self):
        """
        清空转移记录
        """
        TransferHistory.truncate(self._db)

    def add_force(self, **kwargs):
        """
        新增转移历史
        """
        return TransferHistory(**kwargs).create(self._db)