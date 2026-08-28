"""持久副作用 outbox 的应用契约与有限重试 dispatcher。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from typing import Any, Generic, Optional, Protocol, TypeVar, Union

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


class OutboxLeaseLostError(RuntimeError):
    """当前派发 owner 的 attempt 已失效，禁止假报成功或覆盖新 owner。"""


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


class OutboxStager(Protocol):
    """只在业务事务中暂存 durable intent 的最小端口。"""

    def stage(self, intent: OutboxIntent, now: datetime) -> None:
        """在调用方当前事务中暂存意图，不自行提交。"""


class OutboxDispatchStore(Protocol):
    """使用独立短事务认领和结算 durable intent 的最小端口。"""

    def claim(self, now: datetime, lease_until: datetime) -> Optional[ClaimedOutboxMessage]:
        """原子认领一条到期消息。"""

    def claim_by_event_key(
        self,
        event_key: str,
        now: datetime,
        lease_until: datetime,
    ) -> Optional[ClaimedOutboxMessage]:
        """按稳定事件键原子认领一条到期消息。"""

    def complete(
        self,
        message_id: int,
        attempt: int,
        completed_at: datetime,
    ) -> bool:
        """仅由当前 attempt 的 owner 标记完成。"""

    def retry(
        self,
        message_id: int,
        attempt: int,
        *,
        next_retry_at: datetime,
        last_error: str,
        dead: bool,
    ) -> bool:
        """仅由当前 attempt 的 owner 记录退避或 dead-letter。"""

class AsyncOutboxStager(Protocol):
    """只在异步业务事务中暂存 durable intent 的最小端口。"""

    async def stage(self, intent: OutboxIntent, now: datetime) -> None:
        """把 intent 加入调用方当前事务，但不自行提交。"""

class AsyncOutboxDispatchStore(Protocol):
    """使用独立异步短事务认领和结算 intent 的最小端口。"""

    async def claim_by_event_key(
        self,
        event_key: str,
        now: datetime,
        lease_until: datetime,
    ) -> Optional[ClaimedOutboxMessage]:
        """按稳定事件键原子认领一条到期消息。"""

    async def complete(
        self,
        message_id: int,
        attempt: int,
        completed_at: datetime,
    ) -> bool:
        """仅由当前 attempt 的 owner 标记完成。"""

    async def retry(
        self,
        message_id: int,
        attempt: int,
        *,
        next_retry_at: datetime,
        last_error: str,
        dead: bool,
    ) -> bool:
        """仅由当前 attempt 的 owner 记录退避或 dead-letter。"""


class SyncUnitOfWork(Protocol):
    """同步 durable 业务切片的最小事务端口。"""

    def commit(self) -> None:
        """提交业务写入与 outbox intent。"""

    def rollback(self) -> None:
        """回滚业务写入与 outbox intent。"""


@dataclass(frozen=True, slots=True)
class PostCommitResult(Generic[T]):
    """区分已提交业务结果与逐项完成或仍待恢复的后置效果。"""

    value: T
    business_committed: bool
    completed_effects: tuple[str, ...] = ()
    pending_effects: tuple[str, ...] = ()


class PostCommitEffectError(RuntimeError):
    """业务已提交但至少一个后置效果失败，并携带可检查的完成结果。"""

    def __init__(self, result: PostCommitResult[Any], errors: tuple[Exception, ...]):
        """保存结构化完成状态及逐项原始异常。"""
        self.result = result
        self.errors = errors
        super().__init__(str(errors[0]) if errors else "提交后效果执行失败")


def deliver_outbox_effect(
    store: OutboxDispatchStore,
    event_key: str,
    effect: Callable[[], object],
    *,
    clock: Optional[Callable[[], datetime]] = None,
) -> bool:
    """先认领再执行同步效果，并用同一 attempt fencing 结算结果。"""
    now = (clock or (lambda: datetime.now(timezone.utc)))()
    claimed = store.claim_by_event_key(
        event_key,
        now,
        now + timedelta(seconds=OUTBOX_LEASE_SECONDS),
    )
    if claimed is None:
        return False
    try:
        confirmed = effect()
    except Exception as error:
        store.retry(
            claimed.message_id,
            claimed.attempt,
            next_retry_at=now,
            last_error=str(error)[:4000],
            dead=False,
        )
        raise
    if confirmed is False:
        store.retry(
            claimed.message_id,
            claimed.attempt,
            next_retry_at=now,
            last_error="副作用未确认",
            dead=False,
        )
        return False
    if not store.complete(claimed.message_id, claimed.attempt, now):
        raise OutboxLeaseLostError("Outbox 完成凭证已失效")
    return True


async def deliver_async_outbox_effect(
    store: AsyncOutboxDispatchStore,
    event_key: str,
    effect: Callable[[], Awaitable[object]],
    *,
    clock: Optional[Callable[[], datetime]] = None,
) -> bool:
    """先认领再执行异步效果，并用同一 attempt fencing 结算结果。"""
    now = (clock or (lambda: datetime.now(timezone.utc)))()
    claimed = await store.claim_by_event_key(
        event_key,
        now,
        now + timedelta(seconds=OUTBOX_LEASE_SECONDS),
    )
    if claimed is None:
        return False
    try:
        confirmed = await effect()
    except Exception as error:
        await store.retry(
            claimed.message_id,
            claimed.attempt,
            next_retry_at=now,
            last_error=str(error)[:4000],
            dead=False,
        )
        raise
    if confirmed is False:
        await store.retry(
            claimed.message_id,
            claimed.attempt,
            next_retry_at=now,
            last_error="副作用未确认",
            dead=False,
        )
        return False
    if not await store.complete(claimed.message_id, claimed.attempt, now):
        raise OutboxLeaseLostError("Outbox 完成凭证已失效")
    return True


class DurableEventCommand:
    """把一次同步业务写入与可恢复事件 intent 原子提交。"""

    def __init__(
        self,
        unit_of_work: SyncUnitOfWork,
        stager: OutboxStager,
        store: OutboxDispatchStore,
    ) -> None:
        """注入业务事务内 stager 与独立短事务 dispatch store。"""
        self._unit_of_work = unit_of_work
        self._stager = stager
        self._store = store

    def execute(
        self,
        *,
        intent: Optional[Union[OutboxIntent, Callable[[T], OutboxIntent]]],
        stage_business: Callable[[], T],
        publish: Optional[Callable[[], None]],
        after_commit: Optional[Callable[[], None]] = None,
    ) -> PostCommitResult[T]:
        """原子提交业务与 intent，再逐项记录提交后效果完成语义。"""
        resolved_intent: Optional[OutboxIntent] = None
        try:
            result = stage_business()
            if intent is not None:
                resolved_intent = intent(result) if callable(intent) else intent
                self._stager.stage(resolved_intent, datetime.now(timezone.utc))
            self._unit_of_work.commit()
        except Exception:
            self._unit_of_work.rollback()
            raise

        completed: list[str] = []
        pending: list[str] = []
        errors: list[Exception] = []
        if after_commit:
            try:
                after_commit()
                completed.append("after_commit")
            except Exception as error:
                pending.append("after_commit")
                errors.append(error)
        if resolved_intent is not None:
            pending.append(resolved_intent.event_key)
        if resolved_intent is not None and publish is not None:
            now = datetime.now(timezone.utc)
            claimed = self._store.claim_by_event_key(
                resolved_intent.event_key,
                now,
                now + timedelta(seconds=OUTBOX_LEASE_SECONDS),
            )
            if claimed is not None:
                try:
                    publish()
                    settled = self._store.complete(
                        claimed.message_id,
                        claimed.attempt,
                        datetime.now(timezone.utc),
                    )
                    if not settled:
                        raise OutboxLeaseLostError("Outbox 完成凭证已失效")
                    pending.remove(resolved_intent.event_key)
                    completed.append(resolved_intent.event_key)
                except OutboxLeaseLostError as error:
                    errors.append(error)
                except Exception as error:
                    self._store.retry(
                        claimed.message_id,
                        claimed.attempt,
                        next_retry_at=datetime.now(timezone.utc),
                        last_error=str(error)[:4000],
                        dead=False,
                    )
                    errors.append(error)
        execution = PostCommitResult(
            value=result,
            business_committed=True,
            completed_effects=tuple(completed),
            pending_effects=tuple(pending),
        )
        if errors:
            raise PostCommitEffectError(execution, tuple(errors))
        return execution


class OutboxDispatcher:
    """认领并派发 outbox，按 event key 依赖 handler 幂等。"""

    def __init__(
        self,
        repository: OutboxDispatchStore,
        handlers: dict[str, Callable[[ClaimedOutboxMessage], None]],
        *,
        max_attempts: int = 5,
        lease_seconds: int = OUTBOX_LEASE_SECONDS,
        clock: Optional[Callable[[], datetime]] = None,
        close: Optional[Callable[[], None]] = None,
        failure_observer: Optional[Callable[[bool], None]] = None,
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
            if not self._repository.complete(
                message.message_id,
                message.attempt,
                now,
            ):
                raise OutboxLeaseLostError("Outbox 完成凭证已失效")
        except OutboxLeaseLostError:
            raise
        except Exception as error:
            dead = message.attempt >= self._max_attempts
            delay = min(3600, 2 ** max(0, message.attempt - 1))
            settled = self._repository.retry(
                message.message_id,
                message.attempt,
                next_retry_at=now + timedelta(seconds=delay),
                last_error=str(error)[:4000],
                dead=dead,
            )
            if settled:
                self._failure_observer(dead)
            return True
        return True

    def close(self) -> None:
        """释放 dispatcher 工厂创建的短生命周期持久化资源。"""
        self._close()

_configured_dispatcher: Optional[Callable[[], OutboxDispatcher]] = None


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
