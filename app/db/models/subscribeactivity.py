"""
订阅业务活动的持久化记录。
"""

from typing import Any

from sqlalchemy import JSON, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, get_id_column


class SubscribeActivity(Base):
    """
    以独立订阅标识保存活动，完成或删除活动订阅不级联丢失历史。
    """

    id = get_id_column()
    activity_key: Mapped[str] = mapped_column(String(32), nullable=False)
    event_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    source: Mapped[str] = mapped_column(String(24), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    started_at: Mapped[str] = mapped_column(String(40), nullable=False)
    finished_at: Mapped[str | None] = mapped_column(String(40))
    counts: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    tracking: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    __table_args__ = (Index("ix_subscribeactivity_key_event", "activity_key", "event_id"),
                      Index("ix_subscribeactivity_key_time", "activity_key", "started_at", "id"))
