"""订阅删除应用用例及其依赖端口。"""

import inspect
from contextlib import AbstractAsyncContextManager, AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable, Literal, Mapping, Optional, Protocol, cast
from uuid import uuid4

from app.application.outbox import (
    SUBSCRIBE_DELETED_TOPIC,
    AsyncOutboxDispatchStore,
    AsyncOutboxStager,
    OutboxDispatchStore,
    OutboxIntent,
    OutboxStager,
    SyncUnitOfWork,
    deliver_async_outbox_effect,
    deliver_outbox_effect,
)
from app.application.subscription.contract import (
    SubscribeDeletionCandidate,
    SubscriptionStagingPort,
)
from app.runtime.log import logger
from app.schemas.common import JsonData
from app.schemas.event import SubscribeDeletedEventData


@dataclass(frozen=True, slots=True)
class SubscribeDeletionActor:
    """执行订阅删除的用户身份。"""

    username: str
    is_superuser: bool


SubscribeDeletionStatus = Literal["deleted", "not_found", "forbidden"]


class AsyncUnitOfWork(Protocol):
    """订阅写用例使用的异步事务端口。"""

    async def commit(self) -> None:
        """提交当前事务。"""
        ...

    async def rollback(self) -> None:
        """回滚当前事务。"""
        ...


SubscribeDeletedPublisher = Callable[[dict[str, JsonData]], Awaitable[None]]
SubscribeDeletedReporter = Callable[[Mapping[str, JsonData]], object | Awaitable[object]]
SyncSubscribeDeletedPublisher = Callable[[dict[str, JsonData]], None]
SyncSubscribeDeletedReporter = Callable[[Mapping[str, JsonData]], object]


@dataclass(frozen=True, slots=True)
class _SubscribeDeletionEffects:
    """同步和异步入口共用的删除事件、统计与 outbox 意图。"""

    event_payload: dict[str, JsonData]
    report_payload: dict[str, JsonData]
    event_intent: OutboxIntent
    report_intent: OutboxIntent


class DeleteSubscribeCommand:
    """按权限删除订阅，并在提交成功后依次发送事件和统计上报。"""

    def __init__(
        self,
        repository: SubscriptionStagingPort,
        unit_of_work: AsyncUnitOfWork,
        publish_deleted: SubscribeDeletedPublisher,
        report_deleted: SubscribeDeletedReporter,
        outbox: Optional[AsyncOutboxStager] = None,
        dispatch_store: Optional[AsyncOutboxDispatchStore] = None,
    ) -> None:
        """注入数据访问、事务与提交后副作用端口。"""
        self._repository = repository
        self._unit_of_work = unit_of_work
        self._publish_deleted = publish_deleted
        self._report_deleted = report_deleted
        self._outbox = outbox
        self._dispatch_store = dispatch_store

    async def execute(
        self,
        subscribe_id: int,
        actor: SubscribeDeletionActor,
    ) -> bool:
        """
        删除当前用户可访问的订阅。

        返回 False 表示订阅未删除；需要区分具体原因时使用 ``execute_with_status``。
        提交后的事件与上报保持原有顺序，任一副作用失败都会继续向调用方抛出。
        """
        return (await self.execute_with_status(subscribe_id, actor)) == "deleted"

    async def execute_with_status(
        self,
        subscribe_id: int,
        actor: SubscribeDeletionActor,
    ) -> SubscribeDeletionStatus:
        """按权限删除订阅并返回可供 HTTP 层判断的结果状态。"""
        candidate = await self._repository.get_candidate(subscribe_id)
        if candidate is None:
            return "not_found"
        if not can_delete_subscribe(candidate, actor):
            return "forbidden"
        assert candidate is not None

        effects = _build_deletion_effects(
            subscribe_id,
            candidate.event_payload,
        )
        try:
            await self._repository.stage_delete(subscribe_id)
            if self._outbox:
                await self._outbox.stage(
                    effects.event_intent,
                    datetime.now(timezone.utc),
                )
                await self._outbox.stage(
                    effects.report_intent,
                    datetime.now(timezone.utc),
                )
            await self._unit_of_work.commit()
        except Exception:
            await self._unit_of_work.rollback()
            raise

        if self._dispatch_store:
            await deliver_async_outbox_effect(
                self._dispatch_store,
                effects.event_intent.event_key,
                lambda: self._publish_deleted(effects.event_payload),
            )
        else:
            await self._publish_deleted(effects.event_payload)
        # 上报适配器会自行白名单过滤公开字段；传完整删除前快照可保留音乐实体维度，
        # 避免 Agent 与 API 入口收敛后丢失 music_type / total_tracks。
        try:

            async def report() -> object:
                """统一等待同步或异步统计 reporter 的确认结果。"""
                result = self._report_deleted(effects.report_payload)
                return await result if inspect.isawaitable(result) else result

            report_result: object
            if self._dispatch_store:
                report_result = await deliver_async_outbox_effect(
                    self._dispatch_store,
                    effects.report_intent.event_key,
                    report,
                )
            else:
                report_result = await report()
        except Exception as error:
            logger.warning(f"订阅删除统计上报失败，将由后台重试：{error}")
        else:
            if report_result is False:
                logger.warning("订阅删除统计上报未确认，将由后台重试")
        return "deleted"


class SyncDeleteSubscribeCommand:
    """为同步消息入口执行同一订阅删除事务与 durable 副作用协议。"""

    def __init__(
        self,
        repository: SubscriptionStagingPort,
        unit_of_work: SyncUnitOfWork,
        publish_deleted: SyncSubscribeDeletedPublisher,
        report_deleted: SyncSubscribeDeletedReporter,
        outbox: Optional[OutboxStager] = None,
        dispatch_store: Optional[OutboxDispatchStore] = None,
    ) -> None:
        """注入同步数据访问、事务与提交后副作用端口。"""
        self._repository = repository
        self._unit_of_work = unit_of_work
        self._publish_deleted = publish_deleted
        self._report_deleted = report_deleted
        self._outbox = outbox
        self._dispatch_store = dispatch_store

    def execute(
        self,
        subscribe_id: int,
        actor: SubscribeDeletionActor,
    ) -> bool:
        """同步删除当前用户可访问的订阅，并保持事件和统计的可靠投递顺序。"""
        candidate = self._repository.get_candidate_sync(subscribe_id)
        if not can_delete_subscribe(candidate, actor):
            return False
        assert candidate is not None

        effects = _build_deletion_effects(
            subscribe_id,
            candidate.event_payload,
        )
        try:
            self._repository.stage_delete_sync(subscribe_id)
            if self._outbox:
                now = datetime.now(timezone.utc)
                self._outbox.stage(effects.event_intent, now)
                self._outbox.stage(effects.report_intent, now)
            self._unit_of_work.commit()
        except Exception:
            self._unit_of_work.rollback()
            raise

        if self._dispatch_store:
            deliver_outbox_effect(
                self._dispatch_store,
                effects.event_intent.event_key,
                lambda: self._publish_deleted(effects.event_payload),
            )
        else:
            self._publish_deleted(effects.event_payload)
        try:
            report_result: object
            if self._dispatch_store:
                report_result = deliver_outbox_effect(
                    self._dispatch_store,
                    effects.report_intent.event_key,
                    lambda: self._report_deleted(effects.report_payload),
                )
            else:
                report_result = self._report_deleted(effects.report_payload)
        except Exception as error:
            logger.warning(f"订阅删除统计上报失败，将由后台重试：{error}")
        else:
            if report_result is False:
                logger.warning("订阅删除统计上报未确认，将由后台重试")
        return True


def can_delete_subscribe(
    candidate: SubscribeDeletionCandidate | None,
    actor: SubscribeDeletionActor,
) -> bool:
    """判断用户是否拥有目标订阅的删除权限。"""
    if candidate is None:
        return False
    if actor.is_superuser:
        return True
    return bool(candidate.username) and candidate.username == actor.username


def _build_deletion_effects(
    subscribe_id: int,
    subscribe_info: Mapping[str, JsonData],
) -> _SubscribeDeletionEffects:
    """一次性构造两种执行风格共用的事件、上报和 durable intent。"""
    event_payload = build_subscribe_deleted_payload(subscribe_id, subscribe_info)
    event_key = cast(str, event_payload["idempotency_key"])
    report_key = f"{event_key}:report"
    report_payload = dict(subscribe_info)
    report_payload["idempotency_key"] = report_key
    return _SubscribeDeletionEffects(
        event_payload=event_payload,
        report_payload=report_payload,
        event_intent=OutboxIntent(
            event_key=event_key,
            topic=SUBSCRIBE_DELETED_TOPIC,
            payload=event_payload,
        ),
        report_intent=OutboxIntent(
            event_key=report_key,
            topic="subscribe.deleted.report",
            payload={
                "idempotency_key": report_key,
                "subscribe_info": report_payload,
            },
        ),
    )


def build_subscribe_deleted_payload(
    subscribe_id: int,
    subscribe_info: Mapping[str, JsonData],
) -> dict[str, JsonData]:
    """构造兼容旧字段并携带幂等键的订阅删除事件快照。"""
    event_key = f"subscribe.deleted:{subscribe_id}:{uuid4().hex}:v1"
    return cast(
        dict[str, JsonData],
        SubscribeDeletedEventData(
            subscribe_id=subscribe_id,
            subscribe_info=dict(subscribe_info),
            idempotency_key=event_key,
        ).model_dump(mode="json"),
    )


DeleteSubscribeScope = Callable[[], AbstractAsyncContextManager[DeleteSubscribeCommand]]
SyncDeleteSubscribeScope = Callable[[], AbstractContextManager[SyncDeleteSubscribeCommand]]
