"""持久副作用 outbox 模型。"""

from typing import Any, Optional

from sqlalchemy import Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, get_id_column


class OutboxMessage(Base):
    """记录与业务事务原子提交、可认领重试的副作用意图。"""

    id = get_id_column()
    event_key: Mapped[str] = mapped_column(String(255), nullable=False)
    topic: Mapped[str] = mapped_column(String(100), nullable=False)
    payload_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_retry_at: Mapped[str] = mapped_column(String(40), nullable=False)
    lease_until: Mapped[Optional[str]] = mapped_column(String(40))
    last_error: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False)
    completed_at: Mapped[Optional[str]] = mapped_column(String(40))

    __table_args__ = (
        UniqueConstraint("event_key", name="uq_outboxmessage_event_key"),
        Index("ix_outboxmessage_claim", "status", "next_retry_at", "lease_until"),
    )
