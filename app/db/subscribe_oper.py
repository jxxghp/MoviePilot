import time
from typing import Tuple, List, Optional

from app.core.context import MediaInfo, MusicInfo
from app.db import DbOper
from app.db.models.subscribe import Subscribe
from app.db.models.subscribehistory import SubscribeHistory
from app.schemas.types import MUSIC_ENTITY_ALBUM, MediaType
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


def _normalize_year(year: Optional[int | str]) -> Optional[str]:
    """
    订阅表的 year 列为字符串类型，而识别链路的媒体年份可能是数字
    （音乐等来源），写库前统一转换为字符串避免数据库类型错误。
    """
    if year is None:
        return None
    return str(year)


def _music_subscription_fields(mediainfo: MediaInfo | MusicInfo) -> dict:
    """从标准媒体信息提取音乐订阅需要持久化的专辑级字段。"""
    if mediainfo.type != MediaType.MUSIC:
        return {"music_type": None, "total_tracks": None}
    music_type = getattr(mediainfo, "music_type", None)
    return {
        "music_type": music_type,
        "total_tracks": getattr(mediainfo, "total_tracks", None)
        if music_type == MUSIC_ENTITY_ALBUM else None,
    }


class SubscribeOper(DbOper):
    """
    订阅管理
    """

    def add(self, mediainfo: MediaInfo | MusicInfo, **kwargs) -> Tuple[int, str]:
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
            "music_type": getattr(mediainfo, "music_type", None)
            if mediainfo.type == MediaType.MUSIC else None,
            "season": kwargs.get("season"),
            "episode_group": mediainfo.episode_group,
        }
        if username:
            subscribe = Subscribe.exists_by_username(self._db,
                                                     username=username,
                                                     **identity_params)
        else:
            subscribe = Subscribe.exists(self._db, **identity_params)
        kwargs.update({
            "name": mediainfo.title,
            "year": _normalize_year(mediainfo.year),
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
        kwargs.update(_music_subscription_fields(mediainfo))
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

    async def async_add(self, mediainfo: MediaInfo | MusicInfo, **kwargs) -> Tuple[int, str]:
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
            "music_type": getattr(mediainfo, "music_type", None)
            if mediainfo.type == MediaType.MUSIC else None,
            "season": kwargs.get("season"),
            "episode_group": mediainfo.episode_group,
        }
        if username:
            subscribe = await Subscribe.async_exists_by_username(self._db,
                                                                 username=username,
                                                                 **identity_params)
        else:
            subscribe = await Subscribe.async_exists(self._db, **identity_params)
        kwargs.update({
            "name": mediainfo.title,
            "year": _normalize_year(mediainfo.year),
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
        kwargs.update(_music_subscription_fields(mediainfo))
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
            season: Optional[int] = None, episode_group: Optional[str] = None,
            music_type: Optional[str] = None,
    ) -> bool:
        """
        按媒体身份、季号及可选剧集组判断订阅是否存在。
        """
        identity_params = {
            "tmdbid": tmdbid,
            "doubanid": doubanid,
            "bangumiid": bangumiid,
            "anilistid": anilistid,
            "media_source": media_source,
            "media_id": media_id,
            "music_type": music_type,
            "season": season,
            "episode_group": episode_group,
        }
        return bool(Subscribe.exists(self._db, **identity_params))

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
            music_type: Optional[str] = None,
    ) -> Optional[Subscribe]:
        """
        根据条件查询订阅
        """
        return Subscribe.get_by(
            self._db, type, season, tmdbid, doubanid, bangumiid, anilistid,
            media_source, media_id, music_type,
        )

    async def async_get_by(
            self, type: str, season: Optional[str] = None,
            tmdbid: Optional[int] = None, doubanid: Optional[str] = None,
            bangumiid: Optional[int] = None, anilistid: Optional[int] = None,
            media_source: Optional[str] = None, media_id: Optional[str] = None,
            music_type: Optional[str] = None,
    ) -> Optional[Subscribe]:
        """
        根据条件查询订阅
        """
        return await Subscribe.async_get_by(
            self._db, type, season, tmdbid, doubanid, bangumiid, anilistid,
            media_source, media_id, music_type,
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
            season: Optional[int] = None, episode_group: Optional[str] = None,
            music_type: Optional[str] = None,
    ) -> bool:
        """
        按媒体身份、季号及可选剧集组判断订阅历史是否存在。
        """
        identity_params = {
            "tmdbid": tmdbid,
            "doubanid": doubanid,
            "bangumiid": bangumiid,
            "anilistid": anilistid,
            "media_source": media_source,
            "media_id": media_id,
            "music_type": music_type,
            "season": season,
            "episode_group": episode_group,
        }
        return bool(SubscribeHistory.exists(self._db, **identity_params))
