import json
from typing import Optional

from sqlalchemy.orm import Session

from app.db import DbOper
from app.db.models.mediaserver import MediaServerItem


class MediaServerOper(DbOper):
    """
    媒体服务器数据管理
    """

    def __init__(self, db: Session = None):
        super().__init__(db)

    def add(self, **kwargs) -> bool:
        """
        新增媒体服务器数据
        """
        item = MediaServerItem(**kwargs)
        if not item.get_by_itemid(self._db, kwargs.get("item_id")):
            item.create(self._db)
            return True
        return False

    def empty(self, server: Optional[str] = None):
        """
        清空媒体服务器数据
        """
        MediaServerItem.empty(self._db, server)

    def exists(self, **kwargs) -> Optional[MediaServerItem]:
        """
        判断媒体服务器数据是否存在
        """
        if kwargs.get("tmdbid"):
            # 优先按TMDBID查
            item = MediaServerItem.exist_by_tmdbid(self._db, tmdbid=kwargs.get("tmdbid"),
                                                   mtype=kwargs.get("mtype"))
        elif kwargs.get("title"):
            # 按标题、类型、年份查
            item = MediaServerItem.exists_by_title(self._db, title=kwargs.get("title"),
                                                   mtype=kwargs.get("mtype"), year=kwargs.get("year"))
        else:
            return None
        if not item:
            return None

        if kwargs.get("season"):
            # 判断季是否存在
            if not item.seasoninfo:
                return None
            seasoninfo = json.loads(item.seasoninfo) or {}
            if kwargs.get("season") not in seasoninfo.keys():
                return None
        return item

    def get_item_id(self, **kwargs) -> Optional[str]:
        """
        获取媒体服务器数据ID
        """
        item = self.exists(**kwargs)
        if not item:
            return None
        return str(item.item_id)
