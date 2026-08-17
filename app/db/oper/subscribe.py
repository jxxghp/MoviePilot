"""
订阅数据访问。

本模块只收敛针对订阅表的读写。把 MediaInfo / MusicInfo 翻译成一行订阅是订阅业务的
规则，住在 app/application/subscribe.py；这里收到的 payload 已经是纯粹的持久化字段，
因此不 import 任何领域对象。

留在这一层的只有列类型强转与建库时间戳——它们跟着订阅表的列走，换谁来调都一样。
"""
import time
from typing import Any, Tuple, List, Optional

from sqlalchemy import delete as sqlalchemy_delete

from app.application.subscription.delete import SubscribeDeletionCandidate
from app.db.base import DbOper
from app.db.models.subscribe import Subscribe
from app.db.models.subscribehistory import SubscribeHistory
from app.schemas.types import MediaSource

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


def _persistable(payload: dict) -> dict:
    """
    把应用层给的写入字段落成订阅表能收的一行。

    做两件事。一是列类型强转：PostgreSQL 的整型列拒收布尔值、字符串列拒收数字，而
    SQLite 会靠类型亲和悄悄替我们转好——漏了只在生产库上炸，所以放在紧挨建模的地方。
    二是盖建库时间戳：调用方传进来的 date 不作数，否则订阅列表的默认排序与过期清理
    都会读到一个假的建库时间。
    :param payload: 应用层翻译好的写入字段
    :return: 可直接建模的字段字典
    """
    persistable = _normalize_integer_flags(payload)
    persistable["year"] = _normalize_year(persistable.get("year"))
    # search_imdbid 参与搜索分支判定，None 与真值都要归一到 0/1，否则同一列在不同
    # 订阅上会存出三种形态，PG 上还会直接拒写
    persistable["search_imdbid"] = 1 if persistable.get("search_imdbid") else 0
    persistable["date"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    return persistable


class SubscribeOper(DbOper):
    """
    订阅管理
    """

    def _exists(self, identity: dict, username: Optional[str]) -> Optional[Any]:
        """
        按身份查重。
        :param identity: 查重身份
        :param username: 非空时只在该用户的订阅内查
        :return: 命中的订阅行，未命中为 None
        """
        if username:
            return Subscribe.exists_by_username(self._db, username=username, **identity)
        return Subscribe.exists(self._db, **identity)

    async def _async_exists(self, identity: dict, username: Optional[str]) -> Optional[Any]:
        """
        按身份查重（异步）。
        :param identity: 查重身份
        :param username: 非空时只在该用户的订阅内查
        :return: 命中的订阅行，未命中为 None
        """
        if username:
            return await Subscribe.async_exists_by_username(self._db, username=username, **identity)
        return await Subscribe.async_exists(self._db, **identity)

    def add(self, identity: dict, payload: dict,
            username: Optional[str] = None) -> Tuple[int, str]:
        """
        新增订阅：命中既有订阅则原样返回，否则落库后回读。

        回读不是多余的一次查询——写入可能被唯一约束或事务回滚吞掉，此时若报成功，
        调用方会继续按订阅已建立往下走，用户看到「订阅成功」却永远等不到资源。
        :param identity: 查重身份（media_source/media_id/music_type/season/episode_group）
        :param payload: 订阅表的写入字段，媒体翻译由 app/application/subscribe.py 完成
        :param username: 非空时把查重限定在该用户的订阅内
        :return: (订阅 ID, 结果说明)；ID 为 0 表示未新增
        """
        subscribe = self._exists(identity, username)
        if subscribe:
            return subscribe.id, "订阅已存在"
        Subscribe(**_persistable(payload)).create(self._db)
        subscribe = self._exists(identity, username)
        if not subscribe:
            return 0, "新增订阅失败"
        return subscribe.id, "新增订阅成功"

    async def async_add(self, identity: dict, payload: dict,
                        username: Optional[str] = None) -> Tuple[int, str]:
        """
        异步新增订阅，语义与 add 完全一致。
        :param identity: 查重身份（media_source/media_id/music_type/season/episode_group）
        :param payload: 订阅表的写入字段，媒体翻译由 app/application/subscribe.py 完成
        :param username: 非空时把查重限定在该用户的订阅内
        :return: (订阅 ID, 结果说明)；ID 为 0 表示未新增
        """
        subscribe = await self._async_exists(identity, username)
        if subscribe:
            return subscribe.id, "订阅已存在"
        await Subscribe(**_persistable(payload)).async_create(self._db)
        subscribe = await self._async_exists(identity, username)
        if not subscribe:
            return 0, "新增订阅失败"
        return subscribe.id, "新增订阅成功"

    def exists(
            self, media_source: MediaSource, media_id: str,
            season: Optional[int] = None, episode_group: Optional[str] = None,
            music_type: Optional[str] = None,
    ) -> bool:
        """
        按媒体身份、季号及可选剧集组判断订阅是否存在。
        """
        identity_params = {
            "media_source": media_source,
            "media_id": media_id,
            "music_type": music_type,
            "season": season,
            "episode_group": episode_group,
        }
        return bool(Subscribe.exists(self._db, **identity_params))

    def get(self, sid: int) -> Optional[Subscribe]:
        """
        获取订阅
        """
        return Subscribe.get(self._db, rid=sid)

    async def async_get(self, sid: int) -> Optional[Subscribe]:
        """
        获取订阅
        """
        return await Subscribe.async_get(self._db, rid=sid)

    async def get_candidate(
            self,
            subscribe_id: int,
    ) -> Optional[SubscribeDeletionCandidate]:
        """读取订阅删除用例需要的权限字段与完整事件快照。"""
        subscribe = await self.async_get(subscribe_id)
        if not subscribe:
            return None
        values = subscribe.__dict__
        event_payload = {
            column.name: values.get(column.name)
            for column in subscribe.__table__.columns
        }
        return SubscribeDeletionCandidate(
            subscribe_id=subscribe_id,
            username=subscribe.username,
            event_payload=event_payload,
        )

    async def list_candidates_by_identity(
            self,
            media_source: MediaSource,
            media_id: str,
            season: Optional[int],
            music_type: Optional[str],
    ) -> List[SubscribeDeletionCandidate]:
        """按媒体身份读取去重后的订阅删除快照。"""
        subscribes = await Subscribe.async_list_by_media_identity(
            self._db,
            media_source=media_source,
            media_id=media_id,
            music_type=music_type,
        )
        candidates = []
        seen_ids = set()
        for subscribe in subscribes or []:
            subscribe_music_type = getattr(subscribe, "music_type", None)
            if music_type and not (
                    subscribe_music_type == music_type
                    or (music_type == "recording" and subscribe_music_type is None)
            ):
                continue
            if season is not None and subscribe.season != season:
                continue
            if not subscribe.id or subscribe.id in seen_ids:
                continue
            seen_ids.add(subscribe.id)
            values = subscribe.__dict__
            candidates.append(
                SubscribeDeletionCandidate(
                    subscribe_id=subscribe.id,
                    username=subscribe.username,
                    event_payload={
                        column.name: values.get(column.name)
                        for column in subscribe.__table__.columns
                    },
                )
            )
        return candidates

    async def list_search_ids(self, username: str, state: str) -> List[int]:
        """返回用户指定状态的订阅编号，不向应用用例暴露 ORM 列表。"""
        subscribes = await Subscribe.async_list_by_username(
            self._db,
            username,
            state=state,
        )
        return [subscribe.id for subscribe in subscribes if subscribe.id]

    def get_by(
            self, type: str, media_source: MediaSource, media_id: str,
            season: Optional[str] = None,
            music_type: Optional[str] = None,
    ) -> Optional[Subscribe]:
        """
        根据条件查询订阅
        """
        return Subscribe.get_by(
            self._db, type, media_source, media_id, season, music_type,
        )

    async def async_get_by(
            self, type: str, media_source: MediaSource, media_id: str,
            season: Optional[str] = None,
            music_type: Optional[str] = None,
    ) -> Optional[Subscribe]:
        """
        根据条件查询订阅
        """
        return await Subscribe.async_get_by(
            self._db, type, media_source, media_id, season, music_type,
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

    async def stage_delete(self, sid: int) -> None:
        """登记订阅删除但不提交，由 Application UnitOfWork 控制事务边界。"""
        await self._db.execute(
            sqlalchemy_delete(Subscribe).where(Subscribe.id == sid)
        )

    async def async_update(self, sid: int, payload: dict) -> Optional[Subscribe]:
        """
        异步更新订阅。
        """
        subscribe = await self.async_get(sid)
        if subscribe:
            payload = _normalize_integer_flags(payload)
            await subscribe.async_update(self._db, payload)
        return subscribe

    async def async_update_filter_groups(
            self, sid: int, filter_groups: List[str]
    ) -> Optional[Subscribe]:
        """
        异步更新订阅使用的过滤规则组。
        """
        return await self.async_update(sid, {"filter_groups": filter_groups})

    def update(self, sid: int, payload: dict) -> Optional[Subscribe]:
        """
        更新订阅
        """
        subscribe = self.get(sid)
        if subscribe:
            payload = _normalize_integer_flags(payload)
            subscribe.update(self._db, payload)
        return subscribe

    def list_by_username(self, username: str, state: Optional[str] = None,
                         mtype: Optional[str] = None) -> List[Subscribe]:
        """
        获取指定用户的订阅
        """
        return Subscribe.list_by_username(self._db, username=username, state=state, mtype=mtype)

    def list_by_type(self, mtype: str, days: int = 7) -> List[Subscribe]:
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
            self, media_source: MediaSource, media_id: str,
            season: Optional[int] = None, episode_group: Optional[str] = None,
            music_type: Optional[str] = None,
    ) -> bool:
        """
        按媒体身份、季号及可选剧集组判断订阅历史是否存在。
        """
        identity_params = {
            "media_source": media_source,
            "media_id": media_id,
            "music_type": music_type,
            "season": season,
            "episode_group": episode_group,
        }
        return bool(SubscribeHistory.exists(self._db, **identity_params))
