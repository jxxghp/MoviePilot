"""持久副作用 outbox 的应用契约与有限重试 dispatcher。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol


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


class OutboxDispatcher:
    """认领并派发 outbox，按 event key 依赖 handler 幂等。"""

    def __init__(
        self,
        repository: OutboxRepository,
        handlers: dict[str, Callable[[ClaimedOutboxMessage], None]],
        *,
        max_attempts: int = 5,
        lease_seconds: int = 60,
        clock: Callable[[], datetime] | None = None,
        close: Callable[[], None] | None = None,
    ) -> None:
        """注入持久端口、topic handler 和有界重试策略。"""
        self._repository = repository
        self._handlers = handlers
        self._max_attempts = max_attempts
        self._lease_seconds = lease_seconds
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._close = close or (lambda: None)

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
