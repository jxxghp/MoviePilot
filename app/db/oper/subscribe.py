"""
订阅数据访问。

本模块只收敛针对订阅表的读写。把 MediaInfo / MusicInfo 翻译成一行订阅是订阅业务的
规则，住在 app/application/subscription/write.py；这里收到的 payload 已经是纯粹的持久化字段，
因此不 import 任何领域对象。

留在这一层的只有列类型强转与建库时间戳——它们跟着订阅表的列走，换谁来调都一样。

删除/搜索用例需要的订阅身份快照（get_candidate、list_candidates_by_identity）同理只
返回持久化字段字典，把该字典翻译成应用层的 SubscribeDeletionCandidate 是调用方
（app/application/subscription/*.py）的职责，本模块不 import 应用层类型。
"""
import time
from collections.abc import Awaitable, Callable
from typing import Any, Tuple, List, Optional

from sqlalchemy import delete as sqlalchemy_delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.db.base import DbOper
from app.db.models.subscribe import Subscribe
from app.db.models.subscribehistory import SubscribeHistory
from app.schemas.types import MediaSource

INTEGER_FLAG_FIELDS = ("best_version", "best_version_full", "search_imdbid", "manual_total_episode")

AfterCommitEffect = Callable[[int], None]
AsyncAfterCommitEffect = Callable[[int], Awaitable[None]]


class SubscribeStageResult:
    """Oper 暂存结果，按 Application 端口需要暴露最小只读状态。"""

    __slots__ = ("_subscribe_id", "_message", "_created")

    def __init__(self, subscribe_id: int, message: str, created: bool) -> None:
        """保存写入后的订阅 ID、消息和是否创建标志。"""
        self._subscribe_id = subscribe_id
        self._message = message
        self._created = created

    @property
    def subscribe_id(self) -> int:
        """返回已暂存或已存在的订阅 ID。"""
        return self._subscribe_id

    @property
    def message(self) -> str:
        """返回暂存结果的人类可读消息。"""
        return self._message

    @property
    def created(self) -> bool:
        """返回本次暂存是否创建了新记录。"""
        return self._created


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


def _identity_row(subscribe: Subscribe) -> dict:
    """
    读出一行订阅的持久化身份快照：主键、归属用户与全部列值。

    :param subscribe: 已从数据库加载的订阅行
    :return: 含 subscribe_id/username/event_payload 三个键的字典，
        event_payload 为该行全部列的原始值，供调用方翻译为业务对象
    """
    values = subscribe.__dict__
    event_payload = {
        column.name: values.get(column.name)
        for column in subscribe.__table__.columns
    }
    return {
        "subscribe_id": subscribe.id,
        "username": subscribe.username,
        "event_payload": event_payload,
    }


class SubscribeOper(DbOper):
    """
    订阅管理
    """

    @staticmethod
    def _identity_statement(identity: dict, username: Optional[str] = None):
        """构造订阅查重语句，SQL 所有权收口在 Oper。"""
        condition = Subscribe._identity_condition(  # pylint: disable=protected-access
            identity.get("media_source"),
            identity.get("media_id"),
            identity.get("music_type"),
        )
        if condition is None or username == "":
            return None
        statement = select(Subscribe).where(condition)
        if username:
            statement = statement.where(Subscribe.username == username)
        if identity.get("season") is not None:
            statement = statement.where(Subscribe.season == identity["season"])
        return statement.where(
            Subscribe.episode_group == identity.get("episode_group")
        )

    def _exists(self, identity: dict, username: Optional[str]) -> Optional[Any]:
        """
        按身份查重。
        :param identity: 查重身份
        :param username: 非空时只在该用户的订阅内查
        :return: 命中的订阅行，未命中为 None
        """
        if isinstance(self._db, Session):
            statement = self._identity_statement(identity, username)
            if statement is None:
                return None
            return self._db.execute(statement).scalars().first()
        # 旧 SDK 允许无会话构造 Oper；保留其自动短会话行为，但规范入口不得走这里。
        if username:
            return Subscribe.exists_by_username(
                self._db,
                username=username,
                **identity,
            )
        return Subscribe.exists(self._db, **identity)

    async def _async_exists(self, identity: dict, username: Optional[str]) -> Optional[Any]:
        """
        按身份查重（异步）。
        :param identity: 查重身份
        :param username: 非空时只在该用户的订阅内查
        :return: 命中的订阅行，未命中为 None
        """
        if isinstance(self._db, AsyncSession):
            statement = self._identity_statement(identity, username)
            if statement is None:
                return None
            result = await self._db.execute(statement)
            return result.scalars().first()
        # 同步路径一样只为无会话旧入口保留 Model 的自动短会话兼容。
        if username:
            return await Subscribe.async_exists_by_username(
                self._db,
                username=username,
                **identity,
            )
        return await Subscribe.async_exists(self._db, **identity)

    def stage_add(
        self,
        identity: dict,
        payload: dict,
        username: Optional[str] = None,
    ) -> SubscribeStageResult:
        """暂存同步新增并 flush 主键，不提交调用方拥有的事务。"""
        if not isinstance(self._db, Session):
            raise RuntimeError("同步订阅新增需要调用方提供 Session")
        subscribe = self._exists(identity, username)
        if subscribe:
            return SubscribeStageResult(
                subscribe_id=subscribe.id,
                message="订阅已存在",
                created=False,
            )
        subscribe = Subscribe(**_persistable(payload))
        self._db.add(subscribe)
        self._db.flush()
        if not subscribe.id:
            return SubscribeStageResult(0, "新增订阅失败", True)
        return SubscribeStageResult(subscribe.id, "新增订阅成功", True)

    async def async_stage_add(
        self,
        identity: dict,
        payload: dict,
        username: Optional[str] = None,
    ) -> SubscribeStageResult:
        """暂存异步新增并 flush 主键，不提交调用方拥有的事务。"""
        if not isinstance(self._db, AsyncSession):
            raise RuntimeError("异步订阅新增需要调用方提供 AsyncSession")
        subscribe = await self._async_exists(identity, username)
        if subscribe:
            return SubscribeStageResult(
                subscribe_id=subscribe.id,
                message="订阅已存在",
                created=False,
            )
        subscribe = Subscribe(**_persistable(payload))
        self._db.add(subscribe)
        await self._db.flush()
        if not subscribe.id:
            return SubscribeStageResult(0, "新增订阅失败", True)
        return SubscribeStageResult(subscribe.id, "新增订阅成功", True)

    def add(self, identity: dict, payload: dict,
            username: Optional[str] = None,
            after_commit: Optional[AfterCommitEffect] = None) -> Tuple[int, str]:
        """
        新增订阅：命中既有订阅则原样返回，否则落库后回读。

        回读不是多余的一次查询——写入可能被唯一约束或事务回滚吞掉，此时若报成功，
        调用方会继续按订阅已建立往下走，用户看到「订阅成功」却永远等不到资源。
        :param identity: 查重身份（media_source/media_id/music_type/season/episode_group）
        :param payload: 订阅表的写入字段，媒体翻译由 application/subscription/write.py 完成
        :param username: 非空时把查重限定在该用户的订阅内
        :param after_commit: 兼容旧调用方的提交后副作用；新入口由 Application Command 调用
        :return: (订阅 ID, 结果说明)；ID 为 0 表示未新增
        """
        subscribe = self._exists(identity, username)
        if subscribe:
            if after_commit:
                after_commit(subscribe.id)
            return subscribe.id, "订阅已存在"
        self._stage_create(Subscribe(**_persistable(payload)))
        subscribe = self._exists(identity, username)
        if not subscribe:
            return 0, "新增订阅失败"
        if after_commit:
            after_commit(subscribe.id)
        return subscribe.id, "新增订阅成功"

    async def async_add(self, identity: dict, payload: dict,
                        username: Optional[str] = None,
                        after_commit: Optional[AsyncAfterCommitEffect] = None) -> Tuple[int, str]:
        """
        异步新增订阅，语义与 add 完全一致。
        :param identity: 查重身份（media_source/media_id/music_type/season/episode_group）
        :param payload: 订阅表的写入字段，媒体翻译由 application/subscription/write.py 完成
        :param username: 非空时把查重限定在该用户的订阅内
        :param after_commit: 兼容旧调用方的异步提交后副作用
        :return: (订阅 ID, 结果说明)；ID 为 0 表示未新增
        """
        subscribe = await self._async_exists(identity, username)
        if subscribe:
            if after_commit:
                await after_commit(subscribe.id)
            return subscribe.id, "订阅已存在"
        await self._stage_async_create(Subscribe(**_persistable(payload)))
        subscribe = await self._async_exists(identity, username)
        if not subscribe:
            return 0, "新增订阅失败"
        if after_commit:
            await after_commit(subscribe.id)
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
        return bool(self._exists(identity_params, username=None))

    async def async_exists(
            self, media_source: MediaSource, media_id: str,
            season: Optional[int] = None, episode_group: Optional[str] = None,
            music_type: Optional[str] = None,
    ) -> Optional[Subscribe]:
        """异步按媒体身份、季号及可选剧集组读取命中的订阅。"""
        return await self._async_exists(
            {
                "media_source": media_source,
                "media_id": media_id,
                "music_type": music_type,
                "season": season,
                "episode_group": episode_group,
            },
            username=None,
        )

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

    async def async_list_by_media_identity(
        self,
        media_source: MediaSource,
        media_id: str,
        music_type: Optional[str] = None,
    ) -> List[Subscribe]:
        """异步按规范媒体身份读取订阅。"""
        return await Subscribe.async_list_by_media_identity(
            self._db,
            media_source=media_source,
            media_id=media_id,
            music_type=music_type,
        )

    def list_by_media_identity(
        self,
        media_source: MediaSource,
        media_id: str,
        music_type: Optional[str] = None,
    ) -> List[Subscribe]:
        """同步按规范媒体身份读取订阅。"""
        return Subscribe.list_by_media_identity(
            self._db,
            media_source=media_source,
            media_id=media_id,
            music_type=music_type,
        )

    async def get_candidate(
            self,
            subscribe_id: int,
    ) -> Optional[dict]:
        """
        读取一条订阅的持久化身份快照。

        :param subscribe_id: 订阅 ID
        :return: 见 :func:`_identity_row`；订阅不存在时为 None
        """
        subscribe = await self.async_get(subscribe_id)
        if not subscribe:
            return None
        return _identity_row(subscribe)

    async def list_candidates_by_identity(
            self,
            media_source: MediaSource,
            media_id: str,
            season: Optional[int],
            music_type: Optional[str],
    ) -> List[dict]:
        """
        按媒体身份读取去重后的订阅持久化身份快照列表。

        :param media_source: 媒体来源
        :param media_id: 媒体 ID
        :param season: 季号，为 None 时不按季过滤
        :param music_type: 音乐子类型，为 None 时不按子类型过滤
        :return: 见 :func:`_identity_row` 的列表，按订阅 ID 去重
        """
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
            candidates.append(_identity_row(subscribe))
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

    async def async_list_by_username(
        self,
        username: str,
        state: Optional[str] = None,
        mtype: Optional[str] = None,
    ) -> List[Subscribe]:
        """异步按用户获取订阅。"""
        return await Subscribe.async_list_by_username(
            self._db,
            username=username,
            state=state,
            mtype=mtype,
        )

    async def async_list_by_title(
        self,
        title: str,
        season: Optional[int] = None,
    ) -> List[Subscribe]:
        """异步按标题获取订阅，供旧查询测试和迁移调用兼容。"""
        return await Subscribe.async_list_by_title(
            self._db,
            title=title,
            season=season,
        )

    def delete(self, sid: int):
        """
        删除订阅
        """
        self._stage_delete(Subscribe, sid)

    async def async_delete(self, sid: int):
        """
        异步删除订阅。
        """
        await self._stage_async_delete(Subscribe, sid)

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
            await self._stage_async_update(subscribe, payload)
        return subscribe

    async def async_stage_update(
        self,
        sid: int,
        payload: dict,
    ) -> Optional[Subscribe]:
        """在调用方 AsyncSession 中暂存订阅更新并 flush，不提交事务。"""
        if not isinstance(self._db, AsyncSession):
            raise RuntimeError("异步订阅修改需要调用方提供 AsyncSession")
        subscribe = await self.async_get(sid)
        if not subscribe:
            return None
        for key, value in _normalize_integer_flags(payload).items():
            setattr(subscribe, key, value)
        await self._db.flush()
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
            self._stage_update(subscribe, payload)
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
        self._stage_create(subscribe)

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
