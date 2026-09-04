"""
订阅写入应用用例。

本模块把 MediaInfo / MusicInfo 翻译成一行订阅，是订阅表的唯一业务写入口。翻译此前
长在 SubscribeOper.add 上，但取标题、选海报尺寸、判音乐实体、决定哪几个字段构成一条
订阅的身份，都是订阅业务的规则而非数据访问——Oper 只该收敛查询，领域对象不该出现在
它的入参里。搬上来之后 SubscribeOper 收到的是纯粹的持久化字典，与
app/application/history.py 里整理历史的写入路径同构。

留在 Oper 的是列类型强转与建库时间戳：那几步是为 PostgreSQL 的严格类型检查和订阅表
自己的列类型而存在的，跟着列走比跟着调用方走更不容易漂。

字段映射错了不会报错，只会让订阅静静地记错——而搜索、洗版、完成判定、去重全都读这
张表。同步与异步是两份逐字复制的实现，改一条漏一条就是真实缺陷，故翻译与身份构造由
下方 _translate 单点承担，两条链路只在「怎么查、怎么写」上分叉。
"""

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Protocol, Tuple, cast
from uuid import uuid4

from app.application.classification.reference import (
    normalize_classification_reference_payload,
)
from app.application.outbox import SUBSCRIBE_ADDED_TOPIC, OutboxIntent
from app.application.subscription.contract import (
    AfterCommitEffect,
    AsyncAfterCommitEffect,
    SubscriptionIdentity,
    SubscriptionPatch,
    SubscriptionStagingPort,
    SubscriptionWritePort,
    SubscriptionWriteResult,
    subscription_added_event_key,
    subscription_added_notification_key,
    subscription_added_report_key,
)
from app.domain.context import MediaInfo, MusicInfo
from app.schemas.common import JsonData
from app.schemas.media import resolve_media_identity
from app.schemas.types import MUSIC_ENTITY_ALBUM, MediaSource, MediaType

# 身份不完整时的固定返回。身份不全的订阅写进去就是一条永远匹配不上资源的僵尸订阅，
# 而后续按身份去重也会失效，所以必须在查询与建模之前短路
INCOMPLETE_IDENTITY = (0, "未识别到媒体信息，请检查媒体来源和媒体 ID 后重试")


class SubscriptionOutboxStager(Protocol):
    """同步订阅事务暂存 durable intent 的最小端口。"""

    def stage(self, intent: OutboxIntent, now: datetime) -> None:
        """把意图加入当前业务事务。"""


class AsyncSubscriptionOutboxStager(Protocol):
    """异步订阅事务暂存 durable intent 的最小端口。"""

    async def stage(self, intent: OutboxIntent, now: datetime) -> None:
        """把意图加入当前异步业务事务。"""


class UnitOfWork(Protocol):
    """同步订阅新增命令使用的最小事务端口。"""

    def commit(self) -> None:
        """提交当前逻辑操作。"""
        ...

    def rollback(self) -> None:
        """回滚当前逻辑操作。"""
        ...


class AsyncUnitOfWork(Protocol):
    """异步订阅新增命令使用的最小事务端口。"""

    async def commit(self) -> None:
        """提交当前逻辑操作。"""
        ...

    async def rollback(self) -> None:
        """回滚当前逻辑操作。"""
        ...


class CreateSubscriptionCommand:
    """暂存并提交一条同步订阅，重复请求保持历史返回且不产生提交。"""

    def __init__(
        self,
        repository: SubscriptionStagingPort,
        unit_of_work: UnitOfWork,
        outbox: SubscriptionOutboxStager | None = None,
    ) -> None:
        """注入无提交仓储和事务所有者。"""
        self._repository = repository
        self._unit_of_work = unit_of_work
        self._outbox = outbox

    def execute(
        self,
        identity: SubscriptionIdentity,
        payload: SubscriptionPatch,
        username: Optional[str] = None,
        after_commit: Optional[AfterCommitEffect] = None,
        notification: Mapping[str, JsonData] | None = None,
        occurrence_id: Optional[str] = None,
    ) -> Tuple[int, str]:
        """执行同步新增；事务失败回滚，提交后副作用失败不反向回滚。"""
        try:
            staged = self._repository.stage_add(identity, payload, username)
            if staged.created:
                if self._outbox:
                    now = datetime.now(timezone.utc)
                    for intent in _subscribe_added_intents(
                        staged.subscribe_id,
                        payload.to_payload(),
                        username,
                        notification,
                        occurrence_id=occurrence_id,
                    ):
                        self._outbox.stage(intent, now)
                self._unit_of_work.commit()
        except Exception:
            self._unit_of_work.rollback()
            raise
        if staged.subscribe_id and after_commit:
            after_commit(staged.subscribe_id)
        return staged.subscribe_id, staged.message


class AsyncCreateSubscriptionCommand:
    """暂存并提交一条异步订阅，事务成功后才把结果交给副作用调用方。"""

    def __init__(
        self,
        repository: SubscriptionStagingPort,
        unit_of_work: AsyncUnitOfWork,
        outbox: AsyncSubscriptionOutboxStager | None = None,
    ) -> None:
        """注入无提交异步仓储和事务所有者。"""
        self._repository = repository
        self._unit_of_work = unit_of_work
        self._outbox = outbox

    async def execute(
        self,
        identity: SubscriptionIdentity,
        payload: SubscriptionPatch,
        username: Optional[str] = None,
        after_commit: Optional[AsyncAfterCommitEffect] = None,
        notification: Mapping[str, JsonData] | None = None,
        occurrence_id: Optional[str] = None,
    ) -> Tuple[int, str]:
        """执行异步新增；事务失败回滚，提交后副作用失败不反向回滚。"""
        try:
            staged = await self._repository.async_stage_add(
                identity,
                payload,
                username,
            )
            if staged.created:
                if self._outbox:
                    now = datetime.now(timezone.utc)
                    for intent in _subscribe_added_intents(
                        staged.subscribe_id,
                        payload.to_payload(),
                        username,
                        notification,
                        occurrence_id=occurrence_id,
                    ):
                        await self._outbox.stage(intent, now)
                await self._unit_of_work.commit()
        except Exception:
            await self._unit_of_work.rollback()
            raise
        if staged.subscribe_id and after_commit:
            await after_commit(staged.subscribe_id)
        return staged.subscribe_id, staged.message


@dataclass(frozen=True, slots=True)
class SubscriptionCreateRequest:
    """批量新增中的一条订阅写入请求。"""

    identity: SubscriptionIdentity
    payload: SubscriptionPatch
    username: Optional[str] = None
    notification: Mapping[str, JsonData] | None = None
    after_commit: Optional[AsyncAfterCommitEffect] = None
    occurrence_id: str = field(default_factory=lambda: uuid4().hex)


SubscriptionBatchAfterCommitEffect = Callable[
    [int, SubscriptionCreateRequest],
    Awaitable[None],
]


class SubscriptionBatchWritePort(Protocol):
    """Servarr 等批量入口使用的原子异步订阅写端口。"""

    async def async_add(
        self,
        requests: Sequence[SubscriptionCreateRequest],
    ) -> tuple[SubscriptionWriteResult, ...]:
        """在一个事务内新增全部订阅并返回逐条结果。"""
        ...


class SubscriptionBatchWriteError(RuntimeError):
    """批量中的任一订阅无法落库时触发整批回滚。"""


class AsyncCreateSubscriptionBatchCommand:
    """在一个异步 UoW 内暂存多条订阅及各自 durable intents。"""

    def __init__(
        self,
        repository: SubscriptionStagingPort,
        unit_of_work: AsyncUnitOfWork,
        outbox: AsyncSubscriptionOutboxStager,
    ) -> None:
        """注入共享 Session 的 staging port、事务所有者和 outbox。"""
        self._repository = repository
        self._unit_of_work = unit_of_work
        self._outbox = outbox

    async def execute(
        self,
        requests: Sequence[SubscriptionCreateRequest],
        after_commit: Optional[SubscriptionBatchAfterCommitEffect] = None,
    ) -> tuple[SubscriptionWriteResult, ...]:
        """全部暂存成功后提交一次，失败则回滚且不执行外部副作用。"""
        if not requests:
            return ()
        staged_results: list[SubscriptionWriteResult] = []
        created_requests: list[tuple[SubscriptionCreateRequest, SubscriptionWriteResult]] = []
        now = datetime.now(timezone.utc)
        try:
            for request in requests:
                staged = await self._repository.async_stage_add(
                    request.identity,
                    request.payload,
                    request.username,
                )
                if not staged.subscribe_id:
                    raise SubscriptionBatchWriteError(staged.message)
                staged_results.append(staged)
                if not staged.created:
                    continue
                created_requests.append((request, staged))
                for intent in _subscribe_added_intents(
                    staged.subscribe_id,
                    request.payload.to_payload(),
                    request.username,
                    request.notification,
                    occurrence_id=request.occurrence_id,
                ):
                    await self._outbox.stage(intent, now)
            if created_requests:
                await self._unit_of_work.commit()
        except Exception:
            await self._unit_of_work.rollback()
            raise

        if after_commit:
            for request, staged in created_requests:
                await after_commit(staged.subscribe_id, request)
        return tuple(staged_results)


def _subscribe_added_intents(
    subscribe_id: int,
    payload: Mapping[str, JsonData],
    username: str | None,
    notification: Mapping[str, JsonData] | None = None,
    *,
    occurrence_id: Optional[str] = None,
) -> tuple[OutboxIntent, ...]:
    """构造订阅新增事件、通知与外部统计的同事务 durable intents。"""
    resolved_occurrence_id = occurrence_id or uuid4().hex
    event_key = subscription_added_event_key(
        subscribe_id,
        payload,
        occurrence_id=resolved_occurrence_id,
    )
    event_payload = {
        "subscribe_id": subscribe_id,
        "idempotency_key": event_key,
        "username": username,
        "mediainfo": dict(payload),
    }
    intents: list[OutboxIntent] = [
        OutboxIntent(
            event_key=event_key,
            topic=SUBSCRIBE_ADDED_TOPIC,
            payload=event_payload,
        ),
    ]
    if notification:
        intents.append(
            OutboxIntent(
                event_key=subscription_added_notification_key(
                    subscribe_id,
                    payload,
                    occurrence_id=resolved_occurrence_id,
                ),
                topic="subscribe.added.notification",
                payload={
                    "idempotency_key": subscription_added_notification_key(
                        subscribe_id,
                        payload,
                        occurrence_id=resolved_occurrence_id,
                    ),
                    "message": dict(notification),
                },
            )
        )
    intents.append(
        OutboxIntent(
            event_key=subscription_added_report_key(
                subscribe_id,
                payload,
                occurrence_id=resolved_occurrence_id,
            ),
            topic="subscribe.added.report",
            payload={
                "subscribe_info": {
                    **dict(payload),
                    "idempotency_key": subscription_added_report_key(
                        subscribe_id,
                        payload,
                        occurrence_id=resolved_occurrence_id,
                    ),
                }
            },
        )
    )
    return tuple(intents)


def _music_entity(mediainfo: MediaInfo | MusicInfo) -> Optional[str]:
    """
    取音乐实体类型；非音乐媒体一律为空。

    影视订阅带着 music_type 会被音乐去重逻辑当成音乐实体，造成串号。查重身份与写入
    字段都取这一个值，避免两处各算一遍后悄悄分叉。
    :param mediainfo: 识别结果
    :return: 音乐实体类型，非音乐媒体为 None
    """
    if mediainfo.type != MediaType.MUSIC:
        return None
    return getattr(mediainfo, "music_type", None)


def _translate(
    mediainfo: MediaInfo | MusicInfo,
    kwargs: dict[str, JsonData],
) -> Optional[Tuple[SubscriptionIdentity, SubscriptionPatch, Optional[str]]]:
    """
    把识别结果翻译成查重身份与写入字段。

    :param mediainfo: 识别结果
    :param kwargs: 调用方传入的订阅设置，媒体相关的同名字段会被识别结果覆盖
    :return: (查重身份, 写入字段, 限定用户)；媒体身份不完整时返回 None
    """
    owner_scope = bool(kwargs.pop("owner_scope", False))
    username = kwargs.get("username") if owner_scope else None
    media_source, media_id = resolve_media_identity(
        media=mediainfo,
        media_source=kwargs.get("media_source"),
        media_id=kwargs.get("media_id"),
    )
    if not media_source or not media_id:
        return None
    music_type = _music_entity(mediainfo)
    identity = SubscriptionIdentity(
        media_source=MediaSource(media_source),
        media_id=media_id,
        music_type=music_type,
        season=kwargs.get("season") if isinstance(kwargs.get("season"), int) else None,
        episode_group=mediainfo.episode_group,
    )
    payload = cast(
        dict[str, JsonData],
        normalize_classification_reference_payload(
            kwargs,
            media_type=mediainfo.type,
        ),
    )
    payload.update(
        {
            "name": mediainfo.title,
            "year": mediainfo.year,
            "type": mediainfo.type.value,
            "media_source": str(media_source),
            "media_id": media_id,
            "episode_group": mediainfo.episode_group,
            "poster": mediainfo.get_poster_image(),
            "backdrop": mediainfo.get_backdrop_image(),
            "vote": mediainfo.vote_average,
            "description": mediainfo.overview,
            "music_type": music_type,
            # 整专完成判定拿 total_tracks 当分母，单曲带着专辑的曲目数会永远判不到完成
            "total_tracks": getattr(mediainfo, "total_tracks", None) if music_type == MUSIC_ENTITY_ALBUM else None,
        }
    )
    return identity, SubscriptionPatch(payload), username if isinstance(username, str) else None


def build_subscription_create_request(
    mediainfo: MediaInfo | MusicInfo,
    notification: Mapping[str, JsonData] | None = None,
    after_commit: Optional[AsyncAfterCommitEffect] = None,
    occurrence_id: Optional[str] = None,
    **kwargs: JsonData,
) -> Optional[SubscriptionCreateRequest]:
    """把统一订阅字段映射冻结为一条可参与原子批量写入的请求。"""
    translated = _translate(mediainfo, kwargs)
    if translated is None:
        return None
    identity, payload, username = translated
    return SubscriptionCreateRequest(
        identity=identity,
        payload=payload,
        username=username,
        notification=notification,
        after_commit=after_commit,
        occurrence_id=occurrence_id or uuid4().hex,
    )


def add_subscribe(
    mediainfo: MediaInfo | MusicInfo,
    subscribe_oper: SubscriptionWritePort,
    after_commit: Optional[AfterCommitEffect] = None,
    notification: Mapping[str, JsonData] | None = None,
    occurrence_id: Optional[str] = None,
    **kwargs: JsonData,
) -> Tuple[int, str]:
    """
    新增订阅。

    :param mediainfo: 识别结果
    :param subscribe_oper: 调用方显式注入的订阅写入端口
    :param after_commit: 提交后副作用编排；返回 False 表示统计 intent 等待重试
    :param occurrence_id: 本次订阅创建事实的唯一标识，用于避免数据库主键复用造成键冲突
    :param kwargs: 订阅设置；owner_scope 为真时按用户名限定查重范围
    :return: (订阅 ID, 结果说明)；ID 为 0 表示未新增
    """
    translated = _translate(mediainfo, kwargs)
    if translated is None:
        return INCOMPLETE_IDENTITY
    identity, payload, username = translated
    if occurrence_id is None:
        return subscribe_oper.add(
            identity=identity,
            payload=payload,
            username=username,
            after_commit=after_commit,
            notification=notification,
        )
    return subscribe_oper.add(
        identity=identity,
        payload=payload,
        username=username,
        after_commit=after_commit,
        notification=notification,
        occurrence_id=occurrence_id,
    )


async def async_add_subscribe(
    mediainfo: MediaInfo | MusicInfo,
    subscribe_oper: SubscriptionWritePort,
    after_commit: Optional[AsyncAfterCommitEffect] = None,
    notification: Mapping[str, JsonData] | None = None,
    occurrence_id: Optional[str] = None,
    **kwargs: JsonData,
) -> Tuple[int, str]:
    """
    异步新增订阅。

    :param mediainfo: 识别结果
    :param subscribe_oper: 调用方显式注入的订阅写入端口
    :param after_commit: 异步提交后副作用编排；返回 False 表示统计 intent 等待重试
    :param occurrence_id: 本次订阅创建事实的唯一标识，用于避免数据库主键复用造成键冲突
    :param kwargs: 订阅设置；owner_scope 为真时按用户名限定查重范围
    :return: (订阅 ID, 结果说明)；ID 为 0 表示未新增
    """
    translated = _translate(mediainfo, kwargs)
    if translated is None:
        return INCOMPLETE_IDENTITY
    identity, payload, username = translated
    if occurrence_id is None:
        return await subscribe_oper.async_add(
            identity=identity,
            payload=payload,
            username=username,
            after_commit=after_commit,
            notification=notification,
        )
    return await subscribe_oper.async_add(
        identity=identity,
        payload=payload,
        username=username,
        after_commit=after_commit,
        notification=notification,
        occurrence_id=occurrence_id,
    )


__all__ = [
    "AfterCommitEffect",
    "AsyncCreateSubscriptionBatchCommand",
    "AsyncCreateSubscriptionCommand",
    "AsyncAfterCommitEffect",
    "AsyncUnitOfWork",
    "CreateSubscriptionCommand",
    "INCOMPLETE_IDENTITY",
    "SubscriptionBatchWriteError",
    "SubscriptionBatchWritePort",
    "SubscriptionCreateRequest",
    "UnitOfWork",
    "add_subscribe",
    "async_add_subscribe",
    "build_subscription_create_request",
]
