"""
持久化副作用的 Outbox 应用契约与有限重试派发器。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from concurrent.futures import Future
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import Condition
from types import MappingProxyType
from typing import Any, Generic, Optional, Protocol, TypeVar, Union

from app.runtime.log import logger
from app.schemas.types import EventType

T = TypeVar("T")


SUBSCRIBE_ADDED_TOPIC = "subscribe.added"
SUBSCRIBE_MODIFIED_TOPIC = "subscribe.modified"
SUBSCRIBE_DELETED_TOPIC = "subscribe.deleted"
SUBSCRIBE_COMPLETED_TOPIC = "subscribe.complete"
DOWNLOAD_ADDED_TOPIC = "download.added"
DOWNLOAD_NOTIFICATION_TOPIC = "download.added.notification"
DOWNLOAD_MODULE_TOPIC = "download.added.module"
DOWNLOAD_SUBTITLE_TOPIC = "download.added.subtitle"
TRANSFER_COMPLETED_TOPIC = "transfer.completed"
TRANSFER_FAILED_TOPIC = "transfer.failed"
SUBTITLE_TRANSFER_COMPLETED_TOPIC = "transfer.subtitle.completed"
SUBTITLE_TRANSFER_FAILED_TOPIC = "transfer.subtitle.failed"
AUDIO_TRANSFER_COMPLETED_TOPIC = "transfer.audio.completed"
AUDIO_TRANSFER_FAILED_TOPIC = "transfer.audio.failed"
OUTBOX_LEASE_SECONDS = 60
OUTBOX_MAX_INFLIGHT_TOPICS = 20

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
REQUIRED_OUTBOX_TOPICS = frozenset(DURABLE_EVENT_TOPICS.values()) | {
    DOWNLOAD_NOTIFICATION_TOPIC,
    DOWNLOAD_MODULE_TOPIC,
    DOWNLOAD_SUBTITLE_TOPIC,
}


def durable_event_topic(event_type: EventType) -> str:
    """
    返回必须持久化的事件所登记的唯一 Outbox 主题。
    """
    try:
        return DURABLE_EVENT_TOPICS[event_type]
    except KeyError as error:
        raise ValueError(f"事件 {event_type.name} 未登记 durable topic") from error


@dataclass(frozen=True, slots=True)
class OutboxIntent:
    """
    与业务事务一起暂存的版本化副作用意图。

    :param event_key: 业务事件的稳定幂等键，用于避免重复暂存同一意图
    :param topic: 已注册的副作用主题，用于选择派发处理器
    :param payload: 可持久化的副作用业务载荷
    :param payload_version: 载荷协议版本，处理器据此解释数据格式
    """

    event_key: str
    topic: str
    payload: dict[str, Any]
    payload_version: int = 1


@dataclass(frozen=True, slots=True)
class DurableOutboxEffect:
    """
    绑定持久化副作用意图与提交后可选的即时投递函数。

    :param intent: 需要与业务事务一起提交的持久化副作用意图
    :param deliver: 提交后可选的即时投递函数，None 表示仅由恢复流程投递
    """

    intent: OutboxIntent
    deliver: Optional[Callable[[], object]] = None


@dataclass(frozen=True, slots=True)
class ClaimedOutboxMessage:
    """
    派发器已取得租约的稳定消息投影。

    :param message_id: Outbox 持久化消息主键
    :param event_key: 业务事件的稳定幂等键，用于避免重复暂存同一意图
    :param topic: 已注册的副作用主题，用于选择派发处理器
    :param payload: 可持久化的副作用业务载荷
    :param payload_version: 载荷协议版本，处理器据此解释数据格式
    :param attempt: 本次认领序号，续租和结算必须携带同一序号以拒绝陈旧拥有者
    """

    message_id: int
    event_key: str
    topic: str
    payload: dict[str, Any]
    payload_version: int
    attempt: int


class OutboxLeaseLostError(RuntimeError):
    """
    当前派发者的认领序号已失效，禁止报告成功或覆盖后续认领者的状态。
    """


def validate_durable_event_handlers(
    handlers: Mapping[str, Callable[[ClaimedOutboxMessage], None]],
) -> None:
    """
    拒绝使用缺少持久化事件处理器或必需副作用处理器的派发器。
    """
    missing = REQUIRED_OUTBOX_TOPICS - set(handlers)
    if missing:
        raise RuntimeError(
            "Outbox dispatcher 缺少 durable 事件 handler: "
            + ", ".join(sorted(missing))
        )


class OutboxStager(Protocol):
    """
    只在业务事务中暂存持久化副作用意图的最小端口。
    """

    def stage(self, intent: OutboxIntent, now: datetime) -> None:
        """
        在调用方当前事务中暂存意图，不自行提交。

        :param intent: 需要和业务数据一起持久化的版本化副作用意图
        :param now: 意图创建时间，使用带时区的 UTC 时间
        """


class OutboxDispatchStore(Protocol):
    """
    使用独立短事务认领和结算持久化副作用的最小端口。
    """

    def claim(
        self, now: datetime, lease_until: datetime, *,
        topic: Optional[str] = None, exclude_topics: tuple[str, ...] = (),
    ) -> Optional[ClaimedOutboxMessage]:
        """
        原子认领到期消息，可限定主题或排除当前正在执行的主题。

        :param now: 当前带时区的 UTC 时间，用于判断是否到期
        :param lease_until: 本次认领的租约截止时间，使用带时区的 UTC 时间
        :param topic: 仅认领该主题，None 表示不限定主题
        :param exclude_topics: 本轮已在执行或不应再次认领的主题
        :return: 一条已取得租约的到期消息，没有可认领消息时为 None
        """

    def renew(self, message_id: int, attempt: int, lease_until: datetime) -> bool:
        """
        仅为仍处于处理中且认领序号匹配的消息续租。

        :param message_id: 已认领的持久化消息主键
        :param attempt: 当前认领序号，必须与存储中的有效序号一致
        :param lease_until: 本次认领的租约截止时间，使用带时区的 UTC 时间
        :return: 消息仍在处理且认领序号一致时续租并返回 True，否则返回 False
        """

    def claim_by_event_key(
        self,
        event_key: str,
        now: datetime,
        lease_until: datetime,
    ) -> Optional[ClaimedOutboxMessage]:
        """
        按稳定事件键原子认领一条到期消息。

        :param event_key: 业务事件的稳定幂等键
        :param now: 当前带时区的 UTC 时间，用于判断是否到期
        :param lease_until: 本次认领的租约截止时间，使用带时区的 UTC 时间
        :return: 已取得租约的消息；消息未到期、已完成或被其他执行者持有时为 None
        """

    def complete(
        self,
        message_id: int,
        attempt: int,
        completed_at: datetime,
    ) -> bool:
        """
        仅允许持有当前认领序号的派发者标记完成。

        :param message_id: 已认领的持久化消息主键
        :param attempt: 当前认领序号，必须与存储中的有效序号一致
        :param completed_at: 确认完成的带时区 UTC 时间
        :return: 当前认领序号有效且完成状态已写入时为 True；陈旧拥有者返回 False
        """

    def retry(
        self,
        message_id: int,
        attempt: int,
        *,
        next_retry_at: datetime,
        last_error: str,
        dead: bool,
    ) -> bool:
        """
        仅允许持有当前认领序号的派发者记录退避重试或死信。

        :param message_id: 已认领的持久化消息主键
        :param attempt: 当前认领序号，必须与存储中的有效序号一致
        :param next_retry_at: 下次允许重试的带时区 UTC 时间
        :param last_error: 本次失败原因摘要，持久化供排查
        :param dead: 是否停止自动重试并将消息置为死信
        :return: 当前认领序号有效且重试或死信状态已写入时为 True；陈旧拥有者返回 False
        """


class AsyncOutboxStager(Protocol):
    """
    只在异步业务事务中暂存持久化副作用意图的最小端口。
    """

    async def stage(self, intent: OutboxIntent, now: datetime) -> None:
        """
        把副作用意图加入调用方当前事务，但不自行提交。

        :param intent: 需要和业务数据一起持久化的版本化副作用意图
        :param now: 意图创建时间，使用带时区的 UTC 时间
        """


class AsyncOutboxDispatchStore(Protocol):
    """
    使用独立异步短事务认领和结算副作用意图的最小端口。
    """

    async def claim_by_event_key(
        self,
        event_key: str,
        now: datetime,
        lease_until: datetime,
    ) -> Optional[ClaimedOutboxMessage]:
        """
        按稳定事件键原子认领一条到期消息。

        :param event_key: 业务事件的稳定幂等键
        :param now: 当前带时区的 UTC 时间，用于判断是否到期
        :param lease_until: 本次认领的租约截止时间，使用带时区的 UTC 时间
        :return: 已取得租约的消息；消息未到期、已完成或被其他执行者持有时为 None
        """

    async def complete(
        self,
        message_id: int,
        attempt: int,
        completed_at: datetime,
    ) -> bool:
        """
        仅允许持有当前认领序号的派发者标记完成。

        :param message_id: 已认领的持久化消息主键
        :param attempt: 当前认领序号，必须与存储中的有效序号一致
        :param completed_at: 确认完成的带时区 UTC 时间
        :return: 当前认领序号有效且完成状态已写入时为 True；陈旧拥有者返回 False
        """

    async def retry(
        self,
        message_id: int,
        attempt: int,
        *,
        next_retry_at: datetime,
        last_error: str,
        dead: bool,
    ) -> bool:
        """
        仅允许持有当前认领序号的派发者记录退避重试或死信。

        :param message_id: 已认领的持久化消息主键
        :param attempt: 当前认领序号，必须与存储中的有效序号一致
        :param next_retry_at: 下次允许重试的带时区 UTC 时间
        :param last_error: 本次失败原因摘要，持久化供排查
        :param dead: 是否停止自动重试并将消息置为死信
        :return: 当前认领序号有效且重试或死信状态已写入时为 True；陈旧拥有者返回 False
        """


class SyncUnitOfWork(Protocol):
    """
    同步 durable 业务切片的最小事务端口。
    """

    def commit(self) -> None:
        """
        提交业务写入与 Outbox 副作用意图。
        """

    def rollback(self) -> None:
        """
        回滚业务写入与 Outbox 副作用意图。
        """


@dataclass(frozen=True, slots=True)
class PostCommitResult(Generic[T]):
    """
    区分已提交业务结果与逐项完成或仍待恢复的后置效果。

    :param value: 业务操作产生的返回值
    :param business_committed: 业务事务是否已经成功提交
    :param completed_effects: 本次已完成的副作用名称或事件键
    :param pending_effects: 仍需后续恢复执行的副作用名称或事件键
    """

    value: T
    business_committed: bool
    completed_effects: tuple[str, ...] = ()
    pending_effects: tuple[str, ...] = ()


class PostCommitEffectError(RuntimeError):
    """
    业务已提交但至少一个后置效果失败，并携带可检查的完成结果。
    """

    def __init__(self, result: PostCommitResult[Any], errors: tuple[Exception, ...]):
        """
        保存结构化完成状态及逐项原始异常。
        """
        self.result = result
        self.errors = errors
        super().__init__("提交后的相关处理未完成，系统将自动重试")


def deliver_outbox_effect(
    store: OutboxDispatchStore,
    event_key: str,
    effect: Callable[[], object],
    *,
    clock: Optional[Callable[[], datetime]] = None,
) -> bool:
    """
    先认领再执行同步副作用，并以同一认领序号校验结算权。
    """
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
    """
    先认领再执行异步副作用，并以同一认领序号校验结算权。
    """
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
    """
    把一次同步业务写入与可恢复事件意图原子提交。
    """

    def __init__(
        self,
        unit_of_work: SyncUnitOfWork,
        stager: OutboxStager,
        store: OutboxDispatchStore,
    ) -> None:
        """
        注入业务事务内 stager 与独立短事务 dispatch store。
        """
        self._unit_of_work = unit_of_work
        self._stager = stager
        self._store = store

    def execute(
        self,
        *,
        effects: Union[
            tuple[DurableOutboxEffect, ...],
            Callable[[T], tuple[DurableOutboxEffect, ...]],
        ],
        stage_business: Callable[[], T],
    ) -> PostCommitResult[T]:
        """
        原子提交业务与多个具名副作用意图，再认领可即时执行的副作用。
        """
        resolved_effects: tuple[DurableOutboxEffect, ...] = ()
        try:
            result = stage_business()
            resolved_effects = effects(result) if callable(effects) else effects
            event_keys = [effect.intent.event_key for effect in resolved_effects]
            if len(event_keys) != len(set(event_keys)):
                raise ValueError("同一事务不能暂存重复的 outbox event key")
            now = datetime.now(timezone.utc)
            for effect in resolved_effects:
                self._stager.stage(effect.intent, now)
            self._unit_of_work.commit()
        except Exception:
            self._unit_of_work.rollback()
            raise

        completed: list[str] = []
        pending = [effect.intent.event_key for effect in resolved_effects]
        errors: list[Exception] = []
        for effect in resolved_effects:
            if effect.deliver is None:
                continue
            try:
                delivered = deliver_outbox_effect(
                    self._store,
                    effect.intent.event_key,
                    effect.deliver,
                )
                if delivered:
                    pending.remove(effect.intent.event_key)
                    completed.append(effect.intent.event_key)
            except Exception as error:
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
    """
    认领并派发 outbox，按 event key 依赖 handler 幂等。
    """

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
        """
        注入持久端口、topic handler、有界重试策略与失败观测端口。
        """
        self._repository = repository
        self._handlers = handlers
        self._max_attempts = max_attempts
        self._lease_seconds = lease_seconds
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._close = close or (lambda: None)
        self._failure_observer = failure_observer or (lambda _dead: None)

    def claim_one(
        self, *, topic: Optional[str] = None, exclude_topics: tuple[str, ...] = (),
    ) -> Optional[ClaimedOutboxMessage]:
        """
        按 topic 准入约束认领消息；短事务结束后才允许执行外部效果。
        """
        now = self._clock()
        return self._repository.claim(
            now, now + timedelta(seconds=self._lease_seconds),
            topic=topic, exclude_topics=exclude_topics,
        )

    def renew(self, message: ClaimedOutboxMessage) -> None:
        """
        恢复扫描期间续租在途效果；已完成或被接管的 attempt 不会被改写。
        """
        self._repository.renew(
            message.message_id, message.attempt,
            self._clock() + timedelta(seconds=self._lease_seconds),
        )

    def dispatch_one(self) -> bool:
        """
        同步处理一条到期消息；无消息返回 False，失败留待持久化重试。
        """
        message = self.claim_one()
        if message is None:
            return False
        self.dispatch_claimed(message)
        return True

    def dispatch_claimed(self, message: ClaimedOutboxMessage) -> None:
        """
        执行已认领效果，完成与重试时间均取真实结算时刻。
        """
        try:
            self._handlers[message.topic](message)
        except OutboxLeaseLostError:
            raise
        except Exception as error:
            self.retry_claimed(message, error)
            return
        if not self._repository.complete(message.message_id, message.attempt, self._clock()):
            raise OutboxLeaseLostError("Outbox 完成凭证已失效")

    def retry_claimed(self, message: ClaimedOutboxMessage, error: Exception) -> None:
        """
        仅当前认领者可写入有限退避或死信，并记录可定位的消息身份。
        """
        dead = message.attempt >= self._max_attempts
        delay = min(3600, 2 ** min(12, max(0, message.attempt - 1)))
        settled = self._repository.retry(
            message.message_id, message.attempt,
            next_retry_at=self._clock() + timedelta(seconds=delay),
            last_error=str(error)[:4000], dead=dead,
        )
        if settled:
            logger.warning(
                f"Outbox {message.message_id} ({message.topic}) 第 {message.attempt} 次派发失败，"
                f"{'进入死信' if dead else '等待重试'}：{error}"
            )
            self._failure_observer(dead)

    def close(self) -> None:
        """
        释放派发器工厂创建的短生命周期持久化资源。
        """
        self._close()


@dataclass(slots=True)
class _OutboxFlight:
    """
    持有一个主题的派发器、当前租约和实际工作线程的完成句柄。

    :param dispatcher: 拥有本次派发规则和存储端口的派发器
    :param message: 本次认领的消息及租约序号
    :param future: 实际工作线程的完成句柄，尚未提交执行时为 None
    """

    dispatcher: OutboxDispatcher
    message: ClaimedOutboxMessage
    future: Optional[Future[None]] = None


class OutboxRecovery:
    """
    同主题串行、跨主题有界并行恢复，慢事件不占据通知的执行名额。
    """

    def __init__(
        self, provider: Callable[[], OutboxDispatcher],
        submit: Callable[[Callable[[], None]], Future[None]],
        *, max_inflight: int = OUTBOX_MAX_INFLIGHT_TOPICS,
    ) -> None:
        """
        注入短生命周期派发器工厂和宿主管理的线程提交端口。
        """
        self._provider = provider
        self._submit = submit
        self._max_inflight = max_inflight
        self._lock = Condition()
        self._active: dict[str, _OutboxFlight] = {}
        self._stopping = False

    def dispatch(self, limit: int) -> int:
        """
        续租在途效果并启动空闲 topic；返回接收的 topic 数，每个最多处理 limit 条。
        """
        with self._lock:
            if self._stopping:
                return 0
            for flight in self._active.values():
                flight.dispatcher.renew(flight.message)
            accepted = 0
            while accepted < limit and len(self._active) < self._max_inflight:
                dispatcher = self._provider()
                try:
                    message = dispatcher.claim_one(exclude_topics=tuple(self._active))
                except BaseException:
                    dispatcher.close()
                    raise
                if message is None:
                    dispatcher.close()
                    break
                flight = _OutboxFlight(dispatcher, message)
                self._active[message.topic] = flight
                self._start(flight, limit)
                accepted += 1
            return accepted

    def _start(self, flight: _OutboxFlight, limit: int) -> None:
        """
        先登记主题持有者再提交执行，提交失败释放租约；所有回调完成后才释放主题。
        """
        try:
            flight.future = self._submit(lambda: self._run(flight, limit))
        except Exception as error:
            try:
                flight.dispatcher.retry_claimed(flight.message, error)
            finally:
                self._finish(flight)
            raise
        flight.future.add_done_callback(lambda future: self._done(flight, future))

    def _run(self, flight: _OutboxFlight, limit: int) -> None:
        """
        同一工作线程顺序处理该主题；租约即使到期也不启动第二个恢复执行者。
        """
        for index in range(limit):
            flight.dispatcher.dispatch_claimed(flight.message)
            with self._lock:
                if self._stopping or index + 1 >= limit:
                    break
                message = flight.dispatcher.claim_one(topic=flight.message.topic)
                if message is None:
                    break
                flight.message = message

    def _done(self, flight: _OutboxFlight, future: Future[None]) -> None:
        """
        消费后台异常，执行前被取消的任务回到持久化重试队列，资源收尾后再释放持有者。
        """
        try:
            if future.cancelled():
                flight.dispatcher.retry_claimed(flight.message, RuntimeError("Outbox worker 执行前已取消"))
            else:
                future.result()
        except Exception as error:
            logger.error(f"Outbox {flight.message.message_id} 后台派发未完成：{error}")
        finally:
            self._finish(flight)

    def _finish(self, flight: _OutboxFlight) -> None:
        """
        关闭当前工作线程的短生命周期资源，并在锁内撤销同一主题的持有者。
        """
        try:
            flight.dispatcher.close()
        finally:
            with self._lock:
                self._active.pop(flight.message.topic, None)
                self._lock.notify_all()

    def stop(self, timeout: float = 10.0) -> bool:
        """
        停止接收新任务并有限等待实际结束；超时保留持有者，禁止重配或释放下游资源。
        """
        with self._lock:
            self._stopping = True
            return self._lock.wait_for(lambda: not self._active, timeout=max(0.0, timeout))


_configured_dispatcher: Optional[Callable[[], OutboxDispatcher]] = None
_configured_recovery: Optional[OutboxRecovery] = None


def configure_outbox_dispatcher(
    provider: Callable[[], OutboxDispatcher],
    *, submit: Optional[Callable[[Callable[[], None]], Future[None]]] = None,
) -> None:
    """
    登记派发器工厂；生产环境注入托管工作线程，未注入时保留同步入口。
    """
    global _configured_dispatcher, _configured_recovery
    if not stop_outbox_recovery(timeout=0):
        raise RuntimeError("上一 Outbox 恢复 owner 尚未停止")
    _configured_dispatcher = provider
    _configured_recovery = OutboxRecovery(provider, submit) if submit else None


def stop_outbox_recovery(timeout: float = 10.0) -> bool:
    """
    调度器停止后等待 Outbox 工作线程，必须先于插件、模块和数据库释放。
    """
    return _configured_recovery is None or _configured_recovery.stop(timeout)


def reset_outbox_dispatcher() -> None:
    """
    仅在全部在途副作用结束后撤销当前生命周期的工厂与恢复执行者。
    """
    global _configured_dispatcher, _configured_recovery
    if not stop_outbox_recovery(timeout=0):
        raise RuntimeError("Outbox 恢复 owner 尚未停止")
    _configured_dispatcher = None
    _configured_recovery = None


def dispatch_pending_outbox(limit: int = 20) -> int:
    """
    生产恢复按 topic 提交有界批次；同步兼容入口返回实际处理的消息数。
    """
    if _configured_dispatcher is None:
        raise RuntimeError("Outbox dispatcher 尚未配置")
    if _configured_recovery is not None:
        return _configured_recovery.dispatch(limit)
    dispatcher = _configured_dispatcher()
    try:
        processed = 0
        while processed < limit and dispatcher.dispatch_one():
            processed += 1
        return processed
    finally:
        dispatcher.close()
