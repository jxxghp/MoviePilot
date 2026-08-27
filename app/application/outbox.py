"""持久副作用 outbox 的应用契约与有限重试 dispatcher。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from typing import Any, Protocol, TypeVar

from app.schemas.types import EventType


T = TypeVar("T")


SUBSCRIBE_ADDED_TOPIC = "subscribe.added"
SUBSCRIBE_MODIFIED_TOPIC = "subscribe.modified"
SUBSCRIBE_DELETED_TOPIC = "subscribe.deleted"
SUBSCRIBE_COMPLETED_TOPIC = "subscribe.complete"
DOWNLOAD_ADDED_TOPIC = "download.added"
TRANSFER_COMPLETED_TOPIC = "transfer.completed"
TRANSFER_FAILED_TOPIC = "transfer.failed"
SUBTITLE_TRANSFER_COMPLETED_TOPIC = "transfer.subtitle.completed"
SUBTITLE_TRANSFER_FAILED_TOPIC = "transfer.subtitle.failed"
AUDIO_TRANSFER_COMPLETED_TOPIC = "transfer.audio.completed"
AUDIO_TRANSFER_FAILED_TOPIC = "transfer.audio.failed"
OUTBOX_LEASE_SECONDS = 60

DURABLE_EVENT_TOPICS: Mapping[EventType, str] = MappingProxyType({
    EventType.SubscribeAdded: SUBSCRIBE_ADDED_TOPIC,
    EventType.SubscribeModified: SUBSCRIBE_MODIFIED_TOPIC,
    EventType.SubscribeDeleted: SUBSCRIBE_DELETED_TOPIC,
    EventType.SubscribeComplete: SUBSCRIBE_COMPLETED_TOPIC,
    EventType.DownloadAdded: DOWNLOAD_ADDED_TOPIC,
    EventType.TransferComplete: TRANSFER_COMPLETED_TOPIC,
    EventType.TransferFailed: TRANSFER_FAILED_TOPIC,
    EventType.SubtitleTransferComplete: SUBTITLE_TRANSFER_COMPLETED_TOPIC,
    EventType.SubtitleTransferFailed: SUBTITLE_TRANSFER_FAILED_TOPIC,
    EventType.AudioTransferComplete: AUDIO_TRANSFER_COMPLETED_TOPIC,
    EventType.AudioTransferFailed: AUDIO_TRANSFER_FAILED_TOPIC,
})


def durable_event_topic(event_type: EventType) -> str:
    """返回 durable-required 事件唯一登记的 outbox topic。"""
    try:
        return DURABLE_EVENT_TOPICS[event_type]
    except KeyError as error:
        raise ValueError(f"事件 {event_type.name} 未登记 durable topic") from error


@dataclass(frozen=True, slots=True)
class OutboxIntent:
    """与业务事务一起暂存的版本化副作用意图。"""

    event_key: str
    topic: str
    payload: dict[str, Any]
    payload_version: int = 1


@dataclass(frozen=True, slots=True)
class ClaimedOutboxMessage:
    """dispatcher 已获得 lease 的稳定消息投影。"""

    message_id: int
    event_key: str
    topic: str
    payload: dict[str, Any]
    payload_version: int
    attempt: int


def validate_durable_event_handlers(
    handlers: Mapping[str, Callable[[ClaimedOutboxMessage], None]],
) -> None:
    """拒绝缺少任一 durable-required 事件恢复 handler 的 dispatcher。"""
    missing = set(DURABLE_EVENT_TOPICS.values()) - set(handlers)
    if missing:
        raise RuntimeError(
            "Outbox dispatcher 缺少 durable 事件 handler: "
            + ", ".join(sorted(missing))
        )


class OutboxRepository(Protocol):
    """outbox 写入、claim 和终态更新所需的最小端口。"""

    def stage(self, intent: OutboxIntent, now: datetime) -> None:
        """在调用方当前事务中暂存意图，不自行提交。"""

    def claim(self, now: datetime, lease_until: datetime) -> ClaimedOutboxMessage | None:
        """原子认领一条到期消息。"""

    def complete(self, message_id: int, completed_at: datetime) -> None:
        """按消息 ID 标记完成。"""

    def retry(
        self,
        message_id: int,
        *,
        next_retry_at: datetime,
        last_error: str,
        dead: bool,
    ) -> None:
        """记录有限退避或 dead-letter 终态。"""

class AsyncOutboxTransaction(Protocol):
    """异步业务事务暂存并收口 durable intent 的最小端口。"""

    async def stage(self, intent: OutboxIntent, now: datetime) -> None:
        """把 intent 加入调用方当前事务，但不自行提交。"""

    async def complete_by_event_key(
        self,
        event_key: str,
        completed_at: datetime,
    ) -> None:
        """即时投递成功后按稳定幂等键标记 intent 完成。"""


class SyncUnitOfWork(Protocol):
    """同步 durable 业务切片的最小事务端口。"""

    def commit(self) -> None:
        """提交业务写入与 outbox intent。"""

    def rollback(self) -> None:
        """回滚业务写入与 outbox intent。"""


class SyncOutboxTransaction(Protocol):
    """同步业务事务暂存并收口 durable intent 的最小端口。"""

    def stage(self, intent: OutboxIntent, now: datetime) -> None:
        """把 intent 加入调用方事务，但不自行提交。"""

    def claim_by_event_key(
        self,
        event_key: str,
        now: datetime,
        lease_until: datetime,
    ) -> bool:
        """在同步副作用前原子认领 intent，已被其他投递者持有时返回 False。"""

    def complete_by_event_key(
        self,
        event_key: str,
        completed_at: datetime,
    ) -> None:
        """即时投递成功后按幂等键标记 intent 完成。"""


class DurableEventCommand:
    """把一次同步业务写入与可恢复事件 intent 原子提交。"""

    def __init__(
        self,
        unit_of_work: SyncUnitOfWork,
        outbox: SyncOutboxTransaction,
    ) -> None:
        """注入共享同一 Session 的事务与 outbox 端口。"""
        self._unit_of_work = unit_of_work
        self._outbox = outbox

    def execute(
        self,
        *,
        intent: OutboxIntent | Callable[[T], OutboxIntent] | None,
        stage_business: Callable[[], T],
        publish: Callable[[], None] | None,
        after_commit: Callable[[], None] | None = None,
    ) -> T:
        """原子提交业务与可选 intent，再执行可选提交后动作和广播。"""
        resolved_intent: OutboxIntent | None = None
        try:
            result = stage_business()
            if intent is not None:
                resolved_intent = intent(result) if callable(intent) else intent
                self._outbox.stage(resolved_intent, datetime.now(timezone.utc))
            self._unit_of_work.commit()
        except Exception:
            self._unit_of_work.rollback()
            raise

        if after_commit:
            after_commit()
        if publish:
            publish()
        if resolved_intent is not None and publish is not None:
            self._outbox.complete_by_event_key(
                resolved_intent.event_key,
                datetime.now(timezone.utc),
            )
        return result


class OutboxDispatcher:
    """认领并派发 outbox，按 event key 依赖 handler 幂等。"""

    def __init__(
        self,
        repository: OutboxRepository,
        handlers: dict[str, Callable[[ClaimedOutboxMessage], None]],
        *,
        max_attempts: int = 5,
        lease_seconds: int = OUTBOX_LEASE_SECONDS,
        clock: Callable[[], datetime] | None = None,
        close: Callable[[], None] | None = None,
        failure_observer: Callable[[bool], None] | None = None,
    ) -> None:
        """注入持久端口、topic handler、有界重试策略与失败观测端口。"""
        self._repository = repository
        self._handlers = handlers
        self._max_attempts = max_attempts
        self._lease_seconds = lease_seconds
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._close = close or (lambda: None)
        self._failure_observer = failure_observer or (lambda _dead: None)

    def dispatch_one(self) -> bool:
        """处理一条到期消息；无消息返回 False，handler 失败留待重试。"""
        now = self._clock()
        message = self._repository.claim(
            now,
            now + timedelta(seconds=self._lease_seconds),
        )
        if message is None:
            return False
        try:
            handler = self._handlers[message.topic]
            handler(message)
        except Exception as error:
            dead = message.attempt >= self._max_attempts
            delay = min(3600, 2 ** max(0, message.attempt - 1))
            self._repository.retry(
                message.message_id,
                next_retry_at=now + timedelta(seconds=delay),
                last_error=str(error)[:4000],
                dead=dead,
            )
            self._failure_observer(dead)
            return True
        self._repository.complete(message.message_id, now)
        return True

    def close(self) -> None:
        """释放 dispatcher 工厂创建的短生命周期持久化资源。"""
        self._close()

_configured_dispatcher: Callable[[], OutboxDispatcher] | None = None


def configure_outbox_dispatcher(provider: Callable[[], OutboxDispatcher]) -> None:
    """由组合根登记短生命周期 dispatcher 工厂。"""
    global _configured_dispatcher
    _configured_dispatcher = provider


def dispatch_pending_outbox(limit: int = 20) -> int:
    """恢复有限数量到期 intent，供 Scheduler 与启动补偿复用。"""
    if _configured_dispatcher is None:
        raise RuntimeError("Outbox dispatcher 尚未配置")
    dispatcher = _configured_dispatcher()
    try:
        processed = 0
        while processed < limit and dispatcher.dispatch_one():
            processed += 1
        return processed
    finally:
        dispatcher.close()
