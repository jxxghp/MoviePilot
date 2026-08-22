"""启动组合层使用的 SQLAlchemy outbox 持久化适配器。"""

from datetime import datetime

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.outbox import ClaimedOutboxMessage, OutboxIntent
from app.db.base import execute_dml
from app.db.models.outbox import OutboxMessage


def _iso(value: datetime) -> str:
    """将带时区时间统一序列化为可排序 ISO 字符串。"""
    return value.isoformat()


class SqlAlchemyOutboxRepository:
    """使用调用方 Session 原子暂存并条件认领 outbox。"""

    def __init__(self, session: Session) -> None:
        """保存由调用方拥有的 SQLAlchemy Session。"""
        self._session = session

    def stage(self, intent: OutboxIntent, now: datetime) -> None:
        """加入当前事务并 flush，使唯一键冲突在业务 commit 前暴露。"""
        self._session.add(
            OutboxMessage(
                event_key=intent.event_key,
                topic=intent.topic,
                payload_version=intent.payload_version,
                payload=intent.payload,
                status="pending",
                attempt=0,
                next_retry_at=_iso(now),
                created_at=_iso(now),
            )
        )
        self._session.flush()

    def claim(
        self,
        now: datetime,
        lease_until: datetime,
    ) -> ClaimedOutboxMessage | None:
        """条件更新候选行；并发丢失竞争时返回 None。"""
        now_text = _iso(now)
        candidate = self._session.execute(
            select(OutboxMessage)
            .where(
                OutboxMessage.status.in_(("pending", "processing")),
                OutboxMessage.next_retry_at <= now_text,
                or_(
                    OutboxMessage.lease_until.is_(None),
                    OutboxMessage.lease_until <= now_text,
                ),
            )
            .order_by(OutboxMessage.id)
            .limit(1)
        ).scalars().first()
        if candidate is None:
            return None
        next_attempt = candidate.attempt + 1
        claimed = execute_dml(
            self._session,
            update(OutboxMessage)
            .where(
                OutboxMessage.id == candidate.id,
                OutboxMessage.attempt == candidate.attempt,
                or_(
                    OutboxMessage.lease_until.is_(None),
                    OutboxMessage.lease_until <= now_text,
                ),
            )
            .values(
                status="processing",
                attempt=next_attempt,
                lease_until=_iso(lease_until),
            ),
        )
        self._session.commit()
        if not claimed:
            return None
        return ClaimedOutboxMessage(
            message_id=candidate.id,
            event_key=candidate.event_key,
            topic=candidate.topic,
            payload=dict(candidate.payload),
            payload_version=candidate.payload_version,
            attempt=next_attempt,
        )

    def complete(self, message_id: int, completed_at: datetime) -> None:
        """持久化完成终态并释放 lease。"""
        self._session.execute(
            update(OutboxMessage)
            .where(OutboxMessage.id == message_id)
            .values(status="completed", completed_at=_iso(completed_at), lease_until=None)
        )
        self._session.commit()

    def complete_by_event_key(self, event_key: str, completed_at: datetime) -> None:
        """即时 post-commit 全部成功时按幂等键收口对应 intent。"""
        self._session.execute(
            update(OutboxMessage)
            .where(OutboxMessage.event_key == event_key)
            .values(status="completed", completed_at=_iso(completed_at), lease_until=None)
        )
        self._session.commit()

    def retry(
        self,
        message_id: int,
        *,
        next_retry_at: datetime,
        last_error: str,
        dead: bool,
    ) -> None:
        """持久化下一次退避或不可自动重试的 dead 终态。"""
        self._session.execute(
            update(OutboxMessage)
            .where(OutboxMessage.id == message_id)
            .values(
                status="dead" if dead else "pending",
                next_retry_at=_iso(next_retry_at),
                lease_until=None,
                last_error=last_error,
            )
        )
        self._session.commit()


class SqlAlchemyAsyncOutboxStager:
    """只负责把 outbox 意图加入调用方异步事务。"""

    def __init__(self, session: AsyncSession) -> None:
        """保存由异步订阅命令拥有的 Session。"""
        self._session = session

    async def stage(self, intent: OutboxIntent, now: datetime) -> None:
        """暂存并 flush，确保业务行与意图由同一次 commit 决定。"""
        self._session.add(
            OutboxMessage(
                event_key=intent.event_key,
                topic=intent.topic,
                payload_version=intent.payload_version,
                payload=intent.payload,
                status="pending",
                attempt=0,
                next_retry_at=_iso(now),
                created_at=_iso(now),
            )
        )
        await self._session.flush()

    async def complete_by_event_key(
        self,
        event_key: str,
        completed_at: datetime,
    ) -> None:
        """异步 post-commit 全部成功时按幂等键收口 intent。"""
        await self._session.execute(
            update(OutboxMessage)
            .where(OutboxMessage.event_key == event_key)
            .values(status="completed", completed_at=_iso(completed_at), lease_until=None)
        )
        await self._session.commit()
