import time
from typing import Tuple, List, Optional

from app.core.context import MediaInfo
from app.db import DbOper
from app.db.models.subscribe import Subscribe
from app.db.models.subscribehistory import SubscribeHistory
from app.utils.media import resolve_media_identity

INTEGER_FLAG_FIELDS = ("best_version", "best_version_full", "search_imdbid", "manual_total_episode")


def _normalize_integer_flags(payload: dict, fields: Tuple[str, ...] = INTEGER_FLAG_FIELDS) -> dict:
    """
    将历史兼容的布尔开关转换为整型值，避免 PostgreSQL 严格类型检查失败。
    """
    normalized_payload = dict(payload)
    for field in fields:
        if isinstance(normalized_payload.get(field), bool):
            normalized_payload[field] = int(normalized_payload[field])
    return normalized_payload


class SubscribeOper(DbOper):
    """
    订阅管理
    """

    def add(self, mediainfo: MediaInfo, **kwargs) -> Tuple[int, str]:
        """
        新增订阅
        """
        owner_scope = bool(kwargs.pop("owner_scope", False))
        username = kwargs.get("username") if owner_scope else None
        media_source, media_id = resolve_media_identity(
            media=mediainfo,
            source=kwargs.get("media_source"),
            media_id=kwargs.get("media_id"),
        )
        identity_params = {
            "tmdbid": mediainfo.tmdb_id,
            "doubanid": mediainfo.douban_id,
            "bangumiid": mediainfo.bangumi_id,
            "anilistid": mediainfo.anilist_id,
            "media_source": media_source,
            "media_id": media_id,
            "season": kwargs.get("season"),
        }
        if username:
            subscribe = Subscribe.exists_by_username(self._db,
                                                     username=username,
                                                     **identity_params)
        else:
            subscribe = Subscribe.exists(self._db, **identity_params)
        kwargs.update({
            "name": mediainfo.title,
            "year": mediainfo.year,
            "type": mediainfo.type.value,
            "tmdbid": mediainfo.tmdb_id,
            "imdbid": mediainfo.imdb_id,
            "tvdbid": mediainfo.tvdb_id,
            "doubanid": mediainfo.douban_id,
            "bangumiid": mediainfo.bangumi_id,
            "anilistid": mediainfo.anilist_id,
            "media_source": media_source,
            "media_id": media_id,
            "episode_group": mediainfo.episode_group,
            "poster": mediainfo.get_poster_image(),
            "backdrop": mediainfo.get_backdrop_image(),
            "vote": mediainfo.vote_average,
            "description": mediainfo.overview,
            "search_imdbid": 1 if kwargs.get('search_imdbid') else 0,
            "date": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        })
        kwargs = _normalize_integer_flags(kwargs)
        if not subscribe:
            subscribe = Subscribe(**kwargs)
            subscribe.create(self._db)
            # 查询订阅
            if username:
                subscribe = Subscribe.exists_by_username(self._db,
                                                         username=username,
                                                         **identity_params)
            else:
                subscribe = Subscribe.exists(self._db, **identity_params)
            return subscribe.id, "新增订阅成功"
        else:
            return subscribe.id, "订阅已存在"

    async def async_add(self, mediainfo: MediaInfo, **kwargs) -> Tuple[int, str]:
        """
        异步新增订阅
        """
        owner_scope = bool(kwargs.pop("owner_scope", False))
        username = kwargs.get("username") if owner_scope else None
        media_source, media_id = resolve_media_identity(
            media=mediainfo,
            source=kwargs.get("media_source"),
            media_id=kwargs.get("media_id"),
        )
        identity_params = {
            "tmdbid": mediainfo.tmdb_id,
            "doubanid": mediainfo.douban_id,
            "bangumiid": mediainfo.bangumi_id,
            "anilistid": mediainfo.anilist_id,
            "media_source": media_source,
            "media_id": media_id,
            "season": kwargs.get("season"),
        }
        if username:
            subscribe = await Subscribe.async_exists_by_username(self._db,
                                                                 username=username,
                                                                 **identity_params)
        else:
            subscribe = await Subscribe.async_exists(self._db, **identity_params)
        kwargs.update({
            "name": mediainfo.title,
            "year": mediainfo.year,
            "type": mediainfo.type.value,
            "tmdbid": mediainfo.tmdb_id,
            "imdbid": mediainfo.imdb_id,
            "tvdbid": mediainfo.tvdb_id,
            "doubanid": mediainfo.douban_id,
            "bangumiid": mediainfo.bangumi_id,
            "anilistid": mediainfo.anilist_id,
            "media_source": media_source,
            "media_id": media_id,
            "episode_group": mediainfo.episode_group,
            "poster": mediainfo.get_poster_image(),
            "backdrop": mediainfo.get_backdrop_image(),
            "vote": mediainfo.vote_average,
            "description": mediainfo.overview,
            "search_imdbid": 1 if kwargs.get('search_imdbid') else 0,
            "date": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        })
        kwargs = _normalize_integer_flags(kwargs)
        if not subscribe:
            subscribe = Subscribe(**kwargs)
            await subscribe.async_create(self._db)
            # 查询订阅
            if username:
                subscribe = await Subscribe.async_exists_by_username(self._db,
                                                                     username=username,
                                                                     **identity_params)
            else:
                subscribe = await Subscribe.async_exists(self._db, **identity_params)
            return subscribe.id, "新增订阅成功"
        else:
            return subscribe.id, "订阅已存在"

    def exists(
            self, tmdbid: Optional[int] = None, doubanid: Optional[str] = None,
            bangumiid: Optional[int] = None, anilistid: Optional[int] = None,
            media_source: Optional[str] = None, media_id: Optional[str] = None,
            season: Optional[int] = None,
    ) -> bool:
        """
        判断是否存在
        """
        return bool(Subscribe.exists(
            self._db,
            tmdbid=tmdbid,
            doubanid=doubanid,
            bangumiid=bangumiid,
            anilistid=anilistid,
            media_source=media_source,
            media_id=media_id,
            season=season,
        ))

    def get(self, sid: int) -> Subscribe:
        """
        获取订阅
        """
        return Subscribe.get(self._db, rid=sid)

    async def async_get(self, sid: int) -> Subscribe:
        """
        获取订阅
        """
        return await Subscribe.async_get(self._db, rid=sid)

    def get_by(
            self, type: str, season: Optional[str] = None,
            tmdbid: Optional[int] = None, doubanid: Optional[str] = None,
            bangumiid: Optional[int] = None, anilistid: Optional[int] = None,
            media_source: Optional[str] = None, media_id: Optional[str] = None,
    ) -> Optional[Subscribe]:
        """
        根据条件查询订阅
        """
        return Subscribe.get_by(
            self._db, type, season, tmdbid, doubanid, bangumiid, anilistid,
            media_source, media_id,
        )

    async def async_get_by(
            self, type: str, season: Optional[str] = None,
            tmdbid: Optional[int] = None, doubanid: Optional[str] = None,
            bangumiid: Optional[int] = None, anilistid: Optional[int] = None,
            media_source: Optional[str] = None, media_id: Optional[str] = None,
    ) -> Optional[Subscribe]:
        """
        根据条件查询订阅
        """
        return await Subscribe.async_get_by(
            self._db, type, season, tmdbid, doubanid, bangumiid, anilistid,
            media_source, media_id,
        )

    def list(self, state: Optional[str] = None) -> List[Subscribe]:
        """
        获取订阅列表
        """
        if state:
            return Subscribe.get_by_state(self._db, state)
        return Subscribe.list(self._db)

    async def async_list(self, state: Optional[str] = None) -> List[Subscribe]:
        """
        异步获取订阅列表
        """
        if state:
            return await Subscribe.async_get_by_state(self._db, state)
        return await Subscribe.async_list(self._db)

    def delete(self, sid: int):
        """
        删除订阅
        """
        Subscribe.delete(self._db, rid=sid)

    async def async_delete(self, sid: int):
        """
        异步删除订阅。
        """
        await Subscribe.async_delete(self._db, rid=sid)

    async def async_update(self, sid: int, payload: dict) -> Subscribe:
        """
        异步更新订阅。
        """
        subscribe = await self.async_get(sid)
        if subscribe:
            payload = _normalize_integer_flags(payload)
            await subscribe.async_update(self._db, payload)
        return subscribe

    async def async_update_filter_groups(self, sid: int, filter_groups: list) -> Subscribe:
        """
        异步更新订阅使用的过滤规则组。
        """
        return await self.async_update(sid, {"filter_groups": filter_groups})

    def update(self, sid: int, payload: dict) -> Subscribe:
        """
        更新订阅
        """
        subscribe = self.get(sid)
        if subscribe:
            payload = _normalize_integer_flags(payload)
            subscribe.update(self._db, payload)
        return subscribe

    def list_by_tmdbid(self, tmdbid: int, season: Optional[int] = None) -> List[Subscribe]:
        """
        获取指定tmdb_id的订阅
        """
        return Subscribe.get_by_tmdbid(self._db, tmdbid=tmdbid, season=season)

    def list_by_username(self, username: str, state: Optional[str] = None,
                         mtype: Optional[str] = None) -> List[Subscribe]:
        """
        获取指定用户的订阅
        """
        return Subscribe.list_by_username(self._db, username=username, state=state, mtype=mtype)

    def list_by_type(self, mtype: str, days: Optional[int] = 7) -> Subscribe:
        """
        获取指定类型的订阅
        """
        return Subscribe.list_by_type(self._db, mtype=mtype, days=days)

    def add_history(self, **kwargs):
        """
        新增订阅
        """
        # 去除kwargs中 SubscribeHistory 没有的字段
        kwargs = {k: v for k, v in kwargs.items() if hasattr(SubscribeHistory, k)}
        kwargs = _normalize_integer_flags(kwargs)
        # 更新完成订阅时间
        kwargs.update({"date": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())})
        # 去掉主键
        if "id" in kwargs:
            kwargs.pop("id")
        subscribe = SubscribeHistory(**kwargs)
        subscribe.create(self._db)

    def exist_history(
            self, tmdbid: Optional[int] = None, doubanid: Optional[str] = None,
            bangumiid: Optional[int] = None, anilistid: Optional[int] = None,
            media_source: Optional[str] = None, media_id: Optional[str] = None,
            season: Optional[int] = None,
    ) -> bool:
        """
        判断是否存在订阅历史
        """
        return bool(SubscribeHistory.exists(
            self._db,
            tmdbid=tmdbid,
            doubanid=doubanid,
            bangumiid=bangumiid,
            anilistid=anilistid,
            media_source=media_source,
            media_id=media_id,
            season=season,
        ))
