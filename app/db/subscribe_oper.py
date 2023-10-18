import time
from typing import Tuple, List

from app.core.context import MediaInfo
from app.db import DbOper, db_lock
from app.db.models.subscribe import Subscribe


class SubscribeOper(DbOper):
    """
    订阅管理
    """

    @db_lock
    def add(self, mediainfo: MediaInfo, **kwargs) -> Tuple[int, str]:
        """
        新增订阅
        """
        subscribe = Subscribe.exists(self._db, tmdbid=mediainfo.tmdb_id, season=kwargs.get('season'))
        if not subscribe:
            subscribe = Subscribe(name=mediainfo.title,
                                  year=mediainfo.year,
                                  type=mediainfo.type.value,
                                  tmdbid=mediainfo.tmdb_id,
                                  imdbid=mediainfo.imdb_id,
                                  tvdbid=mediainfo.tvdb_id,
                                  poster=mediainfo.get_poster_image(),
                                  backdrop=mediainfo.get_backdrop_image(),
                                  vote=mediainfo.vote_average,
                                  description=mediainfo.overview,
                                  date=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
                                  **kwargs)
            subscribe.create(self._db)
            return subscribe.id, "新增订阅成功"
        else:
            return subscribe.id, "订阅已存在"

    def exists(self, tmdbid: int, season: int) -> bool:
        """
        判断是否存在
        """
        if season:
            return True if Subscribe.exists(self._db, tmdbid=tmdbid, season=season) else False
        else:
            return True if Subscribe.exists(self._db, tmdbid=tmdbid) else False

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

    @db_lock
    def delete(self, sid: int):
        """
        删除订阅
        """
        Subscribe.delete(self._db, rid=sid)

    @db_lock
    def update(self, sid: int, payload: dict) -> Subscribe:
        """
        更新订阅
        """
        subscribe = self.get(sid)
        subscribe.update(self._db, payload)
        return subscribe
