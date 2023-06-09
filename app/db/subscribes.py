from typing import Tuple, List

from sqlalchemy.orm import Session

from app.core.context import MediaInfo
from app.db import SessionLocal
from app.db.models.subscribe import Subscribe
from app.utils.types import MediaType


class Subscribes:
    """
    订阅管理
    """
    _db: Session = None

    def __init__(self, _db=SessionLocal()):
        self._db = _db

    def add(self, mediainfo: MediaInfo, **kwargs) -> Tuple[bool, str]:
        """
        新增订阅
        """
        # 总集数
        if mediainfo.type == MediaType.TV:
            if not kwargs.get('total_episode'):
                total_episode = len(mediainfo.seasons.get(kwargs.get('season') or 1) or [])
                if not total_episode:
                    return False, "未识别到总集数"
                kwargs.update({
                    'total_episode': total_episode
                })
        subscribe = Subscribe(name=mediainfo.title,
                              year=mediainfo.year,
                              type=mediainfo.type.value,
                              tmdbid=mediainfo.tmdb_id,
                              image=mediainfo.get_poster_image(),
                              description=mediainfo.overview,
                              **kwargs)
        if not subscribe.exists(self._db, tmdbid=mediainfo.tmdb_id, season=kwargs.get('season')):
            subscribe.create(self._db)
            return True, "新增订阅成功"
        else:
            return False, "订阅已存在"

    def get(self, sid: int) -> Subscribe:
        """
        获取订阅
        """
        return Subscribe.get(self._db, rid=sid)

    def list(self, state: str = None) -> List[Subscribe]:
        """
        获取订阅列表
        """
        if state:
            return Subscribe.get_by_state(self._db, state)
        return Subscribe.list(self._db)

    def delete(self, sid: int):
        """
        删除订阅
        """
        Subscribe.delete(self._db, rid=sid)

    def update(self, sid: int, payload: dict):
        """
        更新订阅
        """
        subscribe = self.get(sid)
        subscribe.update(self._db, payload)
        return subscribe
