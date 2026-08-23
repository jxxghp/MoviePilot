"""订阅删除应用用例及其依赖端口。"""

from contextlib import AbstractAsyncContextManager, AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timezone
import inspect
from typing import Any, Awaitable, Callable, Mapping, Protocol, cast
from uuid import uuid4

from app.application.outbox import (
    AsyncOutboxTransaction,
    OutboxIntent,
    SyncOutboxTransaction,
    SyncUnitOfWork,
)
from app.schemas.event import SubscribeDeletedEventData


@dataclass(frozen=True)
class SubscribeDeletionActor:
    """执行订阅删除的用户身份。"""

    username: str
    is_superuser: bool


@dataclass(frozen=True)
class SubscribeDeletionCandidate:
    """删除前读取出的订阅快照，不向应用层暴露 ORM 对象。"""

    subscribe_id: int
    username: str | None
    event_payload: Mapping[str, object]


class SubscribeDeletionRepository(Protocol):
    """订阅删除用例需要的最小数据访问端口。"""

    async def get_candidate(
        self,
        subscribe_id: int,
    ) -> SubscribeDeletionCandidate | None:
        """读取订阅及删除事件所需的稳定快照。"""
        ...

    async def stage_delete(self, subscribe_id: int) -> None:
        """把已读取的订阅登记为待删除，但不自行提交事务。"""
        ...


class SyncSubscribeDeletionRepository(Protocol):
    """同步消息入口执行订阅删除所需的最小数据访问端口。"""

    def get_candidate_sync(
        self,
        subscribe_id: int,
    ) -> SubscribeDeletionCandidate | None:
        """读取订阅及删除事件所需的稳定快照。"""
        ...

    def stage_delete_sync(self, subscribe_id: int) -> None:
        """把已读取的订阅登记为待删除，但不自行提交事务。"""
        ...

class AsyncUnitOfWork(Protocol):
    """订阅写用例使用的异步事务端口。"""

    async def commit(self) -> None:
        """提交当前事务。"""
        ...

    async def rollback(self) -> None:
        """回滚当前事务。"""
        ...


SubscribeDeletedPublisher = Callable[[dict[str, Any]], Awaitable[None]]
SubscribeDeletedReporter = Callable[[Mapping[str, object]], object | Awaitable[object]]
SyncSubscribeDeletedPublisher = Callable[[dict[str, Any]], None]
SyncSubscribeDeletedReporter = Callable[[Mapping[str, object]], object]


@dataclass(frozen=True)
class _SubscribeDeletionEffects:
    """同步和异步入口共用的删除事件、统计与 outbox 意图。"""

    event_payload: dict[str, Any]
    report_payload: dict[str, object]
    event_intent: OutboxIntent
    report_intent: OutboxIntent


class DeleteSubscribeCommand:
    """按权限删除订阅，并在提交成功后依次发送事件和统计上报。"""

    def __init__(
        self,
        repository: SubscribeDeletionRepository,
        unit_of_work: AsyncUnitOfWork,
        publish_deleted: SubscribeDeletedPublisher,
        report_deleted: SubscribeDeletedReporter,
        outbox: AsyncOutboxTransaction | None = None,
    ) -> None:
        """注入数据访问、事务与提交后副作用端口。"""
        self._repository = repository
        self._unit_of_work = unit_of_work
        self._publish_deleted = publish_deleted
        self._report_deleted = report_deleted
        self._outbox = outbox

    async def execute(
        self,
        subscribe_id: int,
        actor: SubscribeDeletionActor,
    ) -> bool:
        """
        删除当前用户可访问的订阅。

        返回 False 表示订阅不存在或无权访问；该结果由 API 映射为历史兼容的成功响应。
        提交后的事件与上报保持原有顺序，任一副作用失败都会继续向调用方抛出。
        """
        candidate = await self._repository.get_candidate(subscribe_id)
        if not can_delete_subscribe(candidate, actor):
            return False
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

        await self._publish_deleted(effects.event_payload)
        if self._outbox:
            await self._outbox.complete_by_event_key(
                effects.event_intent.event_key,
                datetime.now(timezone.utc),
            )
        # 上报适配器会自行白名单过滤公开字段；传完整删除前快照可保留音乐实体维度，
        # 避免 Agent 与 API 入口收敛后丢失 music_type / total_tracks。
        report_result = self._report_deleted(effects.report_payload)
        if inspect.isawaitable(report_result):
            report_result = await report_result
        if report_result is False:
            raise RuntimeError("订阅删除统计上报未确认")
        if self._outbox:
            await self._outbox.complete_by_event_key(
                effects.report_intent.event_key,
                datetime.now(timezone.utc),
            )
        return True


class SyncDeleteSubscribeCommand:
    """为同步消息入口执行同一订阅删除事务与 durable 副作用协议。"""

    def __init__(
        self,
        repository: SyncSubscribeDeletionRepository,
        unit_of_work: SyncUnitOfWork,
        publish_deleted: SyncSubscribeDeletedPublisher,
        report_deleted: SyncSubscribeDeletedReporter,
        outbox: SyncOutboxTransaction | None = None,
    ) -> None:
        """注入同步数据访问、事务与提交后副作用端口。"""
        self._repository = repository
        self._unit_of_work = unit_of_work
        self._publish_deleted = publish_deleted
        self._report_deleted = report_deleted
        self._outbox = outbox

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

        self._publish_deleted(effects.event_payload)
        if self._outbox:
            self._outbox.complete_by_event_key(
                effects.event_intent.event_key,
                datetime.now(timezone.utc),
            )
        if self._report_deleted(effects.report_payload) is False:
            raise RuntimeError("订阅删除统计上报未确认")
        if self._outbox:
            self._outbox.complete_by_event_key(
                effects.report_intent.event_key,
                datetime.now(timezone.utc),
            )
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
    subscribe_info: Mapping[str, object],
) -> _SubscribeDeletionEffects:
    """一次性构造两种执行风格共用的事件、上报和 durable intent。"""
    event_payload = build_subscribe_deleted_payload(subscribe_id, subscribe_info)
    event_key = event_payload["idempotency_key"]
    report_key = f"{event_key}:report"
    report_payload = dict(subscribe_info)
    return _SubscribeDeletionEffects(
        event_payload=event_payload,
        report_payload=report_payload,
        event_intent=OutboxIntent(
            event_key=event_key,
            topic="subscribe.deleted",
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
    subscribe_info: Mapping[str, object],
) -> dict[str, Any]:
    """构造兼容旧字段并携带幂等键的订阅删除事件快照。"""
    event_key = f"subscribe.deleted:{subscribe_id}:{uuid4().hex}:v1"
    return cast(
        dict[str, Any],
        SubscribeDeletedEventData(
            subscribe_id=subscribe_id,
            subscribe_info=dict(subscribe_info),
            idempotency_key=event_key,
        ).model_dump(mode="json"),
    )


DeleteSubscribeScope = Callable[[], AbstractAsyncContextManager[DeleteSubscribeCommand]]
SyncDeleteSubscribeScope = Callable[[], AbstractContextManager[SyncDeleteSubscribeCommand]]
_configured_delete_scope: DeleteSubscribeScope | None = None
_configured_sync_delete_scope: SyncDeleteSubscribeScope | None = None


def configure_delete_subscribe_scope(provider: DeleteSubscribeScope) -> None:
    """由启动组合根登记非 HTTP 入口使用的订阅删除事务作用域。"""
    global _configured_delete_scope
    _configured_delete_scope = provider


def get_delete_subscribe_scope() -> AbstractAsyncContextManager[DeleteSubscribeCommand]:
    """返回一次独占会话的订阅删除命令作用域。"""
    if _configured_delete_scope is None:
        raise RuntimeError("订阅删除事务作用域尚未配置")
    return _configured_delete_scope()


def configure_sync_delete_subscribe_scope(provider: SyncDeleteSubscribeScope) -> None:
    """由启动组合根登记同步消息入口使用的订阅删除事务作用域。"""
    global _configured_sync_delete_scope
    _configured_sync_delete_scope = provider


def get_sync_delete_subscribe_scope() -> AbstractContextManager[SyncDeleteSubscribeCommand]:
    """返回一次独占同步会话的订阅删除命令作用域。"""
    if _configured_sync_delete_scope is None:
        raise RuntimeError("同步订阅删除事务作用域尚未配置")
    return _configured_sync_delete_scope()
