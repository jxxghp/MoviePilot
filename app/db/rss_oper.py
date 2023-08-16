from typing import List

from sqlalchemy.orm import Session

from app.db import DbOper
from app.db.models.rss import Rss


class RssOper(DbOper):
    """
    RSS订阅数据管理
    """

    def __init__(self, db: Session = None):
        super().__init__(db)

    def add(self, **kwargs) -> bool:
        """
        新增RSS订阅
        """
        item = Rss(**kwargs)
        item.create(self._db)
        return True

    def exists(self, tmdbid: int, season: int = None):
        """
        判断是否存在
        """
        return Rss.get_by_tmdbid(self._db, tmdbid, season)

    def list(self, rssid: int = None) -> List[Rss]:
        """
        查询所有RSS订阅
        """
        if rssid:
            return [Rss.get(self._db, rssid)]
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

    def update(self, rssid: int, **kwargs) -> bool:
        """
        更新RSS订阅
        """
        item = Rss.get(self._db, rssid)
        if item:
            item.update(self._db, kwargs)
            return True
        return False
