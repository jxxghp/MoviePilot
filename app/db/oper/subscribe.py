"""
订阅数据访问。

本模块只收敛针对订阅表的读写。把 MediaInfo / MusicInfo 翻译成一行订阅是订阅业务的
规则，住在 app/application/subscription/write.py；这里收到的 payload 已经是纯粹的持久化字段，
因此不 import 任何领域对象。

留在这一层的只有列类型强转与建库时间戳——它们跟着订阅表的列走，换谁来调都一样。
"""

import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, List, Optional, Tuple, cast

from sqlalchemy import delete as sqlalchemy_delete
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.application.subscription.contract import SubscribeDeletionCandidate
from app.db.base import DbOper
from app.db.models.subscribe import Subscribe
from app.db.oper.query import (
    descending,
    enum_values,
    execute_page,
    media_identity_conditions,
    music_type_condition,
)
from app.schemas.common import JsonData
from app.schemas.query import QueryPageRequest, QuerySortField, SubscriptionFilter
from app.schemas.types import MediaSource

INTEGER_FLAG_FIELDS = ("best_version", "best_version_full", "search_imdbid", "manual_total_episode")


async def _async_subscription_rows(
    session: AsyncSession,
    statement: Any,
) -> List[Subscribe]:
    """执行订阅列表语句并返回 ORM 行。"""
    result = await session.execute(statement)
    return list(result.scalars().all())


async def _async_scalar(session: AsyncSession, statement: Any) -> int:
    """执行订阅计数语句并返回整数。"""
    result = await session.execute(statement)
    return int(result.scalar_one())

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


def _normalize_integer_flags(
    payload: Mapping[str, JsonData],
    fields: Tuple[str, ...] = INTEGER_FLAG_FIELDS,
) -> dict[str, JsonData]:
    """
    将历史兼容的布尔开关转换为整型值，避免 PostgreSQL 严格类型检查失败。
    """
    normalized_payload = dict(payload)
    for field in fields:
        value = normalized_payload.get(field)
        if isinstance(value, bool):
            normalized_payload[field] = int(value)
    return normalized_payload


def _normalize_year(year: Optional[int | str]) -> Optional[str]:
    """
    订阅表的 year 列为字符串类型，而识别链路的媒体年份可能是数字
    （音乐等来源），写库前统一转换为字符串避免数据库类型错误。
    """
    if year is None:
        return None
    return str(year)


def _persistable(payload: Mapping[str, JsonData]) -> dict[str, JsonData]:
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
    persistable["year"] = _normalize_year(cast(Optional[int | str], persistable.get("year")))
    # search_imdbid 参与搜索分支判定，None 与真值都要归一到 0/1，否则同一列在不同
    # 订阅上会存出三种形态，PG 上还会直接拒写
    persistable["search_imdbid"] = 1 if persistable.get("search_imdbid") else 0
    persistable["date"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    return persistable


def _lookup_values(
    identity: Mapping[str, JsonData],
) -> tuple[MediaSource | str | None, str | None, int | None, str | None, str | None]:
    """把宽 JSON 身份收窄为订阅模型查询参数。"""
    raw_source = identity.get("media_source")
    raw_id = identity.get("media_id")
    raw_season = identity.get("season")
    raw_episode_group = identity.get("episode_group")
    raw_music_type = identity.get("music_type")
    return (
        raw_source if isinstance(raw_source, str) else None,
        str(raw_id) if isinstance(raw_id, (str, int)) else None,
        raw_season if isinstance(raw_season, int) else None,
        raw_episode_group if isinstance(raw_episode_group, str) else None,
        raw_music_type if isinstance(raw_music_type, str) else None,
    )


class SubscribeOper(DbOper):
    """
    订阅管理
    """

    def _exists(
        self,
        identity: Mapping[str, JsonData],
        username: Optional[str],
    ) -> Optional[Subscribe]:
        """
        按身份查重。
        :param identity: 查重身份
        :param username: 非空时只在该用户的订阅内查
        :return: 命中的订阅行，未命中为 None
        """
        if username == "":
            return None
        media_source, media_id, season, episode_group, music_type = _lookup_values(identity)
        if username:
            return cast(
                Optional[Subscribe],
                self._execute_sync_query(
                    lambda session: Subscribe.exists_by_username(
                        session,
                        username=username,
                        media_source=media_source,
                        media_id=media_id,
                        season=season,
                        episode_group=episode_group,
                        music_type=music_type,
                    ),
                ),
            )
        return cast(
            Optional[Subscribe],
            self._execute_sync_query(
                lambda session: Subscribe.exists(
                    session,
                    media_source=media_source,
                    media_id=media_id,
                    season=season,
                    episode_group=episode_group,
                    music_type=music_type,
                )
            ),
        )

    async def _async_exists(
        self,
        identity: Mapping[str, JsonData],
        username: Optional[str],
    ) -> Optional[Subscribe]:
        """
        按身份查重（异步）。
        :param identity: 查重身份
        :param username: 非空时只在该用户的订阅内查
        :return: 命中的订阅行，未命中为 None
        """

        async def query(session: AsyncSession) -> Optional[Subscribe]:
            """在调用方或组合根异步会话中执行订阅查重。"""
            if username == "":
                return None
            media_source, media_id, season, episode_group, music_type = _lookup_values(identity)
            if username:
                return cast(
                    Optional[Subscribe],
                    await Subscribe.async_exists_by_username(
                        session,
                        username=username,
                        media_source=media_source,
                        media_id=media_id,
                        season=season,
                        episode_group=episode_group,
                        music_type=music_type,
                    ),
                )
            return cast(
                Optional[Subscribe],
                await Subscribe.async_exists(
                    session,
                    media_source=media_source,
                    media_id=media_id,
                    season=season,
                    episode_group=episode_group,
                    music_type=music_type,
                ),
            )

        return cast(Optional[Subscribe], await self._execute_async_query(query))

    def stage_add(
        self,
        identity: Mapping[str, JsonData],
        payload: Mapping[str, JsonData],
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
        identity: Mapping[str, JsonData],
        payload: Mapping[str, JsonData],
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

    def add(
        self,
        identity: Mapping[str, JsonData],
        payload: Mapping[str, JsonData],
        username: Optional[str] = None,
        after_commit: Optional[AfterCommitEffect] = None,
    ) -> Tuple[int, str]:
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

    async def async_add(
        self,
        identity: Mapping[str, JsonData],
        payload: Mapping[str, JsonData],
        username: Optional[str] = None,
        after_commit: Optional[AsyncAfterCommitEffect] = None,
    ) -> Tuple[int, str]:
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
        self,
        media_source: MediaSource,
        media_id: str,
        season: Optional[int] = None,
        episode_group: Optional[str] = None,
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
        self,
        media_source: MediaSource,
        media_id: str,
        season: Optional[int] = None,
        episode_group: Optional[str] = None,
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
        return self.get_by_id(sid)

    def get_by_id(self, record_id: int) -> Optional[Subscribe]:
        """按稳定记录 ID 读取单条订阅。"""
        return cast(
            Optional[Subscribe],
            self._execute_sync_query(
                lambda session: session.execute(select(Subscribe).where(Subscribe.id == record_id)).scalars().first()
            ),
        )

    def query(
        self,
        filters: SubscriptionFilter,
        page: QueryPageRequest,
    ) -> tuple[list[Subscribe], int]:
        """按稳定筛选和分页合同读取订阅记录及总数。"""

        def execute(session: Session) -> tuple[list[Subscribe], int]:
            """在同一会话中构造并执行订阅 count/page 查询。"""
            conditions = media_identity_conditions(Subscribe, filters)
            ids = enum_values(filters.ids)
            names = enum_values(filters.names)
            states = enum_values(filters.states)
            usernames = enum_values(filters.usernames)
            media_types = enum_values(filters.media_types)
            if ids:
                conditions.append(Subscribe.id.in_(ids))
            if names:
                conditions.append(Subscribe.name.in_(names))
            if states:
                conditions.append(Subscribe.state.in_(states))
            if usernames:
                conditions.append(Subscribe.username.in_(usernames))
            if media_types:
                conditions.append(Subscribe.type.in_(media_types))
            if filters.season is not None:
                conditions.append(Subscribe.season == filters.season)
            if filters.episode_group is not None:
                conditions.append(Subscribe.episode_group == filters.episode_group)
            music_condition = music_type_condition(
                Subscribe.music_type,
                filters.music_type,
            )
            if music_condition is not None:
                conditions.append(music_condition)

            count_statement = select(func.count(Subscribe.id))
            page_statement = select(Subscribe)
            if conditions:
                count_statement = count_statement.where(*conditions)
                page_statement = page_statement.where(*conditions)
            descending_order = descending(page)
            if page.sort.field == QuerySortField.ID:
                primary = Subscribe.id.desc() if descending_order else Subscribe.id.asc()
                secondary = Subscribe.date.desc() if descending_order else Subscribe.date.asc()
            else:
                primary = Subscribe.date.desc().nullslast() if descending_order else Subscribe.date.asc().nullsfirst()
                secondary = Subscribe.id.desc() if descending_order else Subscribe.id.asc()
            page_statement = page_statement.order_by(primary, secondary)
            return cast(
                tuple[list[Subscribe], int],
                execute_page(session, count_statement, page_statement, page),
            )

        return self._execute_sync_query(execute)

    async def async_get(self, sid: int) -> Optional[Subscribe]:
        """
        获取订阅
        """
        return await self._execute_async_query(lambda session: Subscribe.async_get(session, sid))

    async def async_list_by_media_identity(
        self,
        media_source: MediaSource,
        media_id: str,
        music_type: Optional[str] = None,
    ) -> List[Subscribe]:
        """异步按规范媒体身份读取订阅。"""
        return cast(
            List[Subscribe],
            await self._execute_async_query(
                lambda session: Subscribe.async_list_by_media_identity(
                    session,
                    media_source=media_source,
                    media_id=media_id,
                    music_type=music_type,
                )
            ),
        )

    def list_by_media_identity(
        self,
        media_source: MediaSource,
        media_id: str,
        music_type: Optional[str] = None,
    ) -> List[Subscribe]:
        """同步按规范媒体身份读取订阅。"""
        return cast(
            List[Subscribe],
            self._execute_sync_query(
                lambda session: Subscribe.list_by_media_identity(
                    session,
                    media_source=media_source,
                    media_id=media_id,
                    music_type=music_type,
                )
            ),
        )

    async def get_candidate(
        self,
        subscribe_id: int,
    ) -> Optional[SubscribeDeletionCandidate]:
        """读取订阅删除用例需要的权限字段与完整事件快照。"""
        subscribe = await self.async_get(subscribe_id)
        return self._deletion_candidate(subscribe_id, subscribe)

    def get_candidate_sync(
        self,
        subscribe_id: int,
    ) -> Optional[SubscribeDeletionCandidate]:
        """同步读取订阅删除用例需要的权限字段与完整事件快照。"""
        return self._deletion_candidate(subscribe_id, self.get(subscribe_id))

    @staticmethod
    def _deletion_candidate(
        subscribe_id: int,
        subscribe: Optional[Subscribe],
    ) -> Optional[SubscribeDeletionCandidate]:
        """把 ORM 行投影为同步和异步删除命令共用的稳定快照。"""
        if not subscribe:
            return None
        values = subscribe.__dict__
        return SubscribeDeletionCandidate(
            subscribe_id=subscribe_id,
            username=subscribe.username,
            event_payload={column.name: values.get(column.name) for column in subscribe.__table__.columns},
        )

    async def list_candidates_by_identity(
        self,
        media_source: MediaSource,
        media_id: str,
        season: Optional[int],
        music_type: Optional[str],
    ) -> List[SubscribeDeletionCandidate]:
        """按媒体身份读取去重后的订阅删除快照。"""
        subscribes = await self.async_list_by_media_identity(media_source, media_id, music_type)
        candidates = []
        seen_ids = set()
        for subscribe in subscribes or []:
            subscribe_music_type = getattr(subscribe, "music_type", None)
            if music_type and not (
                subscribe_music_type == music_type or (music_type == "recording" and subscribe_music_type is None)
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
                    event_payload={column.name: values.get(column.name) for column in subscribe.__table__.columns},
                )
            )
        return candidates

    async def list_search_ids(self, username: Optional[str], state: str) -> List[int]:
        """返回用户或管理员全局范围内指定状态的订阅编号。"""
        subscribes = (
            await self.async_list_by_username(username, state=state)
            if username is not None
            else await self.async_list(state=state)
        )
        return [subscribe.id for subscribe in subscribes if subscribe.id]

    def get_by(
        self,
        type: str,
        media_source: MediaSource,
        media_id: str,
        season: Optional[int] = None,
        music_type: Optional[str] = None,
    ) -> Optional[Subscribe]:
        """
        根据条件查询订阅
        """
        return cast(
            Optional[Subscribe],
            self._execute_sync_query(
                lambda session: Subscribe.get_by(
                    session,
                    type=type,
                    media_source=media_source,
                    media_id=media_id,
                    season=season,
                    music_type=music_type,
                )
            ),
        )

    async def async_get_by(
        self,
        type: str,
        media_source: MediaSource,
        media_id: str,
        season: Optional[int] = None,
        music_type: Optional[str] = None,
    ) -> Optional[Subscribe]:
        """
        根据条件查询订阅
        """
        return cast(
            Optional[Subscribe],
            await self._execute_async_query(
                lambda session: Subscribe.async_get_by(
                    session,
                    type=type,
                    media_source=media_source,
                    media_id=media_id,
                    season=season,
                    music_type=music_type,
                )
            ),
        )

    def list(self, state: Optional[str] = None) -> List[Subscribe]:
        """
        获取订阅列表
        """
        return cast(List[Subscribe], self._execute_sync_query(lambda session: Subscribe.get_by_state(session, state)))

    async def async_list(
        self,
        state: Optional[str] = None,
        page: Optional[int] = None,
        count: Optional[int] = None,
    ) -> List[Subscribe]:
        """按可选状态和数据库窗口异步获取订阅列表。"""
        statement = select(Subscribe).order_by(Subscribe.id)
        if state:
            statement = statement.where(Subscribe.state.in_(state.split(",")))
        if page is not None and count is not None:
            statement = statement.offset((page - 1) * count).limit(count)
        return cast(
            List[Subscribe],
            await self._execute_async_query(
                lambda session: _async_subscription_rows(session, statement)
            ),
        )

    async def async_list_by_username(
        self,
        username: str,
        state: Optional[str] = None,
        mtype: Optional[str] = None,
        page: Optional[int] = None,
        count: Optional[int] = None,
    ) -> List[Subscribe]:
        """按用户筛选和数据库窗口异步获取订阅。"""
        statement = select(Subscribe).where(Subscribe.username == username).order_by(Subscribe.id)
        if state:
            statement = statement.where(Subscribe.state == state)
        if mtype:
            statement = statement.where(Subscribe.type == mtype)
        if page is not None and count is not None:
            statement = statement.offset((page - 1) * count).limit(count)
        return cast(
            List[Subscribe],
            await self._execute_async_query(
                lambda session: _async_subscription_rows(session, statement)
            ),
        )

    async def async_count(
        self,
        state: Optional[str] = None,
        username: Optional[str] = None,
        mtype: Optional[str] = None,
    ) -> int:
        """按公开列表筛选条件返回订阅精确总数。"""
        statement = select(func.count()).select_from(Subscribe)
        if state:
            statement = statement.where(Subscribe.state.in_(state.split(",")))
        if username:
            statement = statement.where(Subscribe.username == username)
        if mtype:
            statement = statement.where(Subscribe.type == mtype)
        return int(
            await self._execute_async_query(
                lambda session: _async_scalar(session, statement)
            )
        )

    async def async_list_by_title(
        self,
        title: str,
        season: Optional[int] = None,
    ) -> List[Subscribe]:
        """在 Oper 会话边界内异步按标题获取订阅。"""
        return cast(
            List[Subscribe],
            await self._execute_async_query(
                lambda session: Subscribe.async_list_by_title(
                    session,
                    title=title,
                    season=season,
                )
            ),
        )

    def delete(self, sid: int) -> None:
        """
        删除订阅
        """
        self._stage_delete(Subscribe, sid)

    async def async_delete(self, sid: int) -> None:
        """
        异步删除订阅。
        """
        await self._stage_async_delete(Subscribe, sid)

    async def stage_delete(self, sid: int) -> None:
        """登记订阅删除但不提交，由 Application UnitOfWork 控制事务边界。"""
        if not isinstance(self._db, AsyncSession):
            raise RuntimeError("异步订阅删除需要调用方提供 AsyncSession")
        await self._db.execute(sqlalchemy_delete(Subscribe).where(Subscribe.id == sid))

    def stage_delete_sync(self, sid: int) -> None:
        """同步登记订阅删除但不提交，由 Application UnitOfWork 控制事务边界。"""
        if not isinstance(self._db, Session):
            raise RuntimeError("同步订阅删除需要调用方提供 Session")
        self._db.execute(sqlalchemy_delete(Subscribe).where(Subscribe.id == sid))

    async def async_update(self, sid: int, payload: Mapping[str, JsonData]) -> Optional[Subscribe]:
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
        payload: Mapping[str, JsonData],
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

    async def async_update_filter_groups(self, sid: int, filter_groups: List[str]) -> Optional[Subscribe]:
        """
        异步更新订阅使用的过滤规则组。
        """
        return await self.async_update(sid, {"filter_groups": filter_groups})

    def update(self, sid: int, payload: Mapping[str, JsonData]) -> Optional[Subscribe]:
        """
        更新订阅
        """
        subscribe = self.get(sid)
        if subscribe:
            payload = _normalize_integer_flags(payload)
            self._stage_update(subscribe, payload)
        return subscribe

    def list_by_username(
        self, username: str, state: Optional[str] = None, mtype: Optional[str] = None
    ) -> List[Subscribe]:
        """
        获取指定用户的订阅
        """
        return cast(
            List[Subscribe],
            self._execute_sync_query(
                lambda session: Subscribe.list_by_username(
                    session,
                    username=username,
                    state=state,
                    mtype=mtype,
                )
            ),
        )

    def list_by_type(self, mtype: str, days: int = 7) -> List[Subscribe]:
        """
        获取指定类型的订阅
        """
        return cast(
            List[Subscribe], self._execute_sync_query(lambda session: Subscribe.list_by_type(session, mtype, days))
        )
