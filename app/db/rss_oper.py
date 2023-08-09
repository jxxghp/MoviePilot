from typing import List

from app.db import DbOper, SessionLocal
from app.db.models.rss import Rss


class RssOper(DbOper):
    """
    RSS订阅数据管理
    """

    def __init__(self, db=SessionLocal()):
        super().__init__(db)

    def add(self, **kwargs) -> bool:
        """
        新增RSS订阅
        """
        item = Rss(**kwargs)
        if not item.get_by_tmdbid(self._db, tmdbid=kwargs.get("tmdbid"),
                                  season=kwargs.get("season")):
            item.create(self._db)
            return True
        return False

    def list(self) -> List[Rss]:
        """
        查询所有RSS订阅
        """
        return Rss.list(self._db)

    def delete(self, rssid: int) -> bool:
        """
        删除RSS订阅
        """
        item = Rss.get(self._db, rssid)
        if item:
            item.delete(self._db)
            return True
        return False
