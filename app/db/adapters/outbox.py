"""Application outbox 暂存与派发端口的 SQLAlchemy 适配器。"""

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime
from typing import Optional

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from sqlalchemy.sql import Select
from sqlalchemy.sql.dml import Update

from app.application.outbox import ClaimedOutboxMessage, OutboxIntent
from app.db.base import execute_dml
from app.db.models.outbox import OutboxMessage


def _iso(value: datetime) -> str:
    """将带时区时间统一序列化为可排序 ISO 字符串。"""
    return value.isoformat()


def _message(model: OutboxMessage, attempt: int) -> ClaimedOutboxMessage:
    """把已认领 ORM 行复制为脱离会话的稳定消息。"""
    return ClaimedOutboxMessage(
        message_id=model.id,
        event_key=model.event_key,
        topic=model.topic,
        payload=dict(model.payload),
        payload_version=model.payload_version,
        attempt=attempt,
    )


def _claim_query(
    now_text: str,
    event_key: Optional[str] = None,
) -> Select[tuple[OutboxMessage]]:
    """构造到期且 lease 可取得的候选查询。"""
    statement = select(OutboxMessage).where(
        OutboxMessage.status.in_(("pending", "processing")),
        OutboxMessage.next_retry_at <= now_text,
        or_(
            OutboxMessage.lease_until.is_(None),
            OutboxMessage.lease_until <= now_text,
        ),
    )
    if event_key is not None:
        statement = statement.where(OutboxMessage.event_key == event_key)
    return statement.order_by(OutboxMessage.id).limit(1)


def _claim_update(
    candidate: OutboxMessage,
    now_text: str,
    lease_until: datetime,
) -> Update:
    """构造带旧 attempt fencing 的条件认领更新。"""
    return (
        update(OutboxMessage)
        .where(
            OutboxMessage.id == candidate.id,
            OutboxMessage.attempt == candidate.attempt,
            OutboxMessage.status.in_(("pending", "processing")),
            OutboxMessage.next_retry_at <= now_text,
            or_(
                OutboxMessage.lease_until.is_(None),
                OutboxMessage.lease_until <= now_text,
            ),
        )
        .values(
            status="processing",
            attempt=candidate.attempt + 1,
            lease_until=_iso(lease_until),
        )
    )


class SqlAlchemyOutboxStager:
    """只在调用方同步业务事务中暂存 durable intent。"""

    def __init__(self, session: Session) -> None:
        """保存业务事务拥有的同步 Session。"""
        self._session = session

    def stage(self, intent: OutboxIntent, now: datetime) -> None:
        """加入当前事务并 flush，使唯一键冲突在 commit 前暴露。"""
        self._session.add(_outbox_model(intent, now))
        self._session.flush()


class SqlAlchemyAsyncOutboxStager:
    """只在调用方异步业务事务中暂存 durable intent。"""

    def __init__(self, session: AsyncSession) -> None:
        """保存业务事务拥有的异步 Session。"""
        self._session = session

    async def stage(self, intent: OutboxIntent, now: datetime) -> None:
        """暂存并 flush，业务行与 intent 由同一次 commit 决定。"""
        self._session.add(_outbox_model(intent, now))
        await self._session.flush()


class SqlAlchemyOutboxDispatchStore:
    """用独立同步短事务认领并结算 outbox 消息。"""

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        """保存每次操作创建独立 Session 的工厂。"""
        self._session_factory = session_factory

    def claim(
        self,
        now: datetime,
        lease_until: datetime,
    ) -> Optional[ClaimedOutboxMessage]:
        """原子认领最早一条到期消息。"""
        return self._claim(now, lease_until)

    def claim_by_event_key(
        self,
        event_key: str,
        now: datetime,
        lease_until: datetime,
    ) -> Optional[ClaimedOutboxMessage]:
        """按稳定事件键原子认领到期消息。"""
        return self._claim(now, lease_until, event_key)

    def _claim(
        self,
        now: datetime,
        lease_until: datetime,
        event_key: Optional[str] = None,
    ) -> Optional[ClaimedOutboxMessage]:
        """在独立事务中以 compare-and-swap 取得 lease。"""
        now_text = _iso(now)
        with self._session_factory() as session:
            candidate = session.execute(_claim_query(now_text, event_key)).scalars().first()
            if candidate is None:
                return None
            next_attempt = candidate.attempt + 1
            claimed = execute_dml(
                session,
                _claim_update(candidate, now_text, lease_until),
            )
            session.commit()
            return _message(candidate, next_attempt) if claimed else None

    def complete(
        self,
        message_id: int,
        attempt: int,
        completed_at: datetime,
    ) -> bool:
        """仅允许当前 attempt 的 processing owner 标记完成。"""
        with self._session_factory() as session:
            changed = execute_dml(
                session,
                update(OutboxMessage)
                .where(
                    OutboxMessage.id == message_id,
                    OutboxMessage.status == "processing",
                    OutboxMessage.attempt == attempt,
                )
                .values(
                    status="completed",
                    completed_at=_iso(completed_at),
                    lease_until=None,
                ),
            )
            session.commit()
            return bool(changed)

    def retry(
        self,
        message_id: int,
        attempt: int,
        *,
        next_retry_at: datetime,
        last_error: str,
        dead: bool,
    ) -> bool:
        """仅允许当前 attempt 的 owner 释放 lease 或写入 dead 终态。"""
        with self._session_factory() as session:
            changed = execute_dml(
                session,
                update(OutboxMessage)
                .where(
                    OutboxMessage.id == message_id,
                    OutboxMessage.status == "processing",
                    OutboxMessage.attempt == attempt,
                )
                .values(
                    status="dead" if dead else "pending",
                    next_retry_at=_iso(next_retry_at),
                    lease_until=None,
                    last_error=last_error,
                ),
            )
            session.commit()
            return bool(changed)


class SqlAlchemyAsyncOutboxDispatchStore:
    """用独立异步短事务认领并结算 outbox 消息。"""

    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    ) -> None:
        """保存每次操作创建独立异步 Session 的工厂。"""
        self._session_factory = session_factory

    async def claim_by_event_key(
        self,
        event_key: str,
        now: datetime,
        lease_until: datetime,
    ) -> Optional[ClaimedOutboxMessage]:
        """按稳定事件键原子认领到期消息。"""
        now_text = _iso(now)
        async with self._session_factory() as session:
            candidate = (await session.execute(_claim_query(now_text, event_key))).scalars().first()
            if candidate is None:
                return None
            next_attempt = candidate.attempt + 1
            result = await session.execute(_claim_update(candidate, now_text, lease_until))
            await session.commit()
            return _message(candidate, next_attempt) if result.rowcount else None

    async def complete(
        self,
        message_id: int,
        attempt: int,
        completed_at: datetime,
    ) -> bool:
        """仅允许当前 attempt 的 processing owner 标记完成。"""
        async with self._session_factory() as session:
            result = await session.execute(
                update(OutboxMessage)
                .where(
                    OutboxMessage.id == message_id,
                    OutboxMessage.status == "processing",
                    OutboxMessage.attempt == attempt,
                )
                .values(
                    status="completed",
                    completed_at=_iso(completed_at),
                    lease_until=None,
                )
            )
            await session.commit()
            return bool(result.rowcount)

    async def retry(
        self,
        message_id: int,
        attempt: int,
        *,
        next_retry_at: datetime,
        last_error: str,
        dead: bool,
    ) -> bool:
        """仅允许当前 attempt 的 owner 释放 lease 或写入 dead 终态。"""
        async with self._session_factory() as session:
            result = await session.execute(
                update(OutboxMessage)
                .where(
                    OutboxMessage.id == message_id,
                    OutboxMessage.status == "processing",
                    OutboxMessage.attempt == attempt,
                )
                .values(
                    status="dead" if dead else "pending",
                    next_retry_at=_iso(next_retry_at),
                    lease_until=None,
                    last_error=last_error,
                )
            )
            await session.commit()
            return bool(result.rowcount)


def _outbox_model(intent: OutboxIntent, now: datetime) -> OutboxMessage:
    """构造由业务事务持有的新 outbox ORM 行。"""
    payload = dict(intent.payload)
    payload["idempotency_key"] = intent.event_key
    return OutboxMessage(
        event_key=intent.event_key,
        topic=intent.topic,
        payload_version=intent.payload_version,
        payload=payload,
        status="pending",
        attempt=0,
        next_retry_at=_iso(now),
        created_at=_iso(now),
    )
