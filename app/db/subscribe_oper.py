import json
import time
from typing import Tuple, List

from app.core.context import MediaInfo
from app.db import DbOper
from app.db.models.subscribe import Subscribe


class SubscribeOper(DbOper):
    """
    订阅管理
    """

    def add(self, mediainfo: MediaInfo, **kwargs) -> Tuple[int, str]:
        """
        新增订阅
        """
        subscribe = Subscribe.exists(self._db,
                                     tmdbid=mediainfo.tmdb_id,
                                     doubanid=mediainfo.douban_id,
                                     season=kwargs.get('season'))
        if not subscribe:
            if kwargs.get("sites") and not isinstance(kwargs.get("sites"), str):
                kwargs["sites"] = json.dumps(kwargs.get("sites"))

            subscribe = Subscribe(name=mediainfo.title,
                                  year=mediainfo.year,
                                  type=mediainfo.type.value,
                                  tmdbid=mediainfo.tmdb_id,
                                  imdbid=mediainfo.imdb_id,
                                  tvdbid=mediainfo.tvdb_id,
                                  doubanid=mediainfo.douban_id,
                                  bangumiid=mediainfo.bangumi_id,
                                  poster=mediainfo.get_poster_image(),
                                  backdrop=mediainfo.get_backdrop_image(),
                                  vote=mediainfo.vote_average,
                                  description=mediainfo.overview,
                                  date=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
                                  **kwargs)
            subscribe.create(self._db)
            # 查询订阅
            subscribe = Subscribe.exists(self._db,
                                         tmdbid=mediainfo.tmdb_id,
                                         doubanid=mediainfo.douban_id,
                                         season=kwargs.get('season'))
            return subscribe.id, "新增订阅成功"
        else:
            return subscribe.id, "订阅已存在"

    def exists(self, tmdbid: int = None, doubanid: str = None, season: int = None) -> bool:
        """
        判断是否存在
        """
        if tmdbid:
            if season:
                return True if Subscribe.exists(self._db, tmdbid=tmdbid, season=season) else False
            else:
                return True if Subscribe.exists(self._db, tmdbid=tmdbid) else False
        elif doubanid:
            return True if Subscribe.exists(self._db, doubanid=doubanid) else False
        return False

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

    def update(self, sid: int, payload: dict) -> Subscribe:
        """
        更新订阅
        """
        subscribe = self.get(sid)
        subscribe.update(self._db, payload)
        return subscribe

    def list_by_tmdbid(self, tmdbid: int, season: int = None) -> List[Subscribe]:
        """
        获取指定tmdb_id的订阅
        """
        return Subscribe.get_by_tmdbid(self._db, tmdbid=tmdbid, season=season)

    def list_by_username(self, username: str, state: str = None, mtype: str = None) -> List[Subscribe]:
        """
        获取指定用户的订阅
        """
        return Subscribe.list_by_username(self._db, username=username, state=state, mtype=mtype)

    def list_by_type(self, mtype: str, days: int = 7) -> Subscribe:
        """
        获取指定类型的订阅
        """
        return Subscribe.list_by_type(self._db, mtype=mtype, days=days)
