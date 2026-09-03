"""订阅搜索持久批次与任务模型。"""

from typing import Optional

from sqlalchemy import Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, get_id_column


class SubscriptionSearchBatch(Base):
    """记录一次订阅搜索请求及其可观察聚合终态。"""

    id = get_id_column()
    batch_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    finished_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cancelled_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    skipped_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cancel_requested: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(40), nullable=False)
    started_at: Mapped[Optional[str]] = mapped_column(String(40))
    finished_at: Mapped[Optional[str]] = mapped_column(String(40))
    last_error: Mapped[Optional[str]] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint("batch_id", name="uq_subscriptionsearchbatch_batch_id"),
        Index("ix_subscriptionsearchbatch_state_created", "state", "created_at", "id"),
    )


class SubscriptionSearchTask(Base):
    """记录一个具有 single-flight、租约和恢复游标的订阅搜索任务。"""

    id = get_id_column()
    task_id: Mapped[str] = mapped_column(String(64), nullable=False)
    batch_id: Mapped[str] = mapped_column(String(64), nullable=False)
    subscription_id: Mapped[int] = mapped_column(Integer, nullable=False)
    active_key: Mapped[Optional[str]] = mapped_column(String(128))
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    phase: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    current_site_id: Mapped[Optional[int]] = mapped_column(Integer)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cancel_requested: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lease_owner: Mapped[Optional[str]] = mapped_column(String(128))
    lease_token: Mapped[Optional[str]] = mapped_column(String(64))
    lease_expires_at: Mapped[Optional[str]] = mapped_column(String(40))
    available_at: Mapped[Optional[str]] = mapped_column(String(40))
    created_at: Mapped[str] = mapped_column(String(40), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(40), nullable=False)
    started_at: Mapped[Optional[str]] = mapped_column(String(40))
    finished_at: Mapped[Optional[str]] = mapped_column(String(40))
    last_error: Mapped[Optional[str]] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint("task_id", name="uq_subscriptionsearchtask_task_id"),
        UniqueConstraint("active_key", name="uq_subscriptionsearchtask_active_key"),
        Index(
            "ix_subscriptionsearchtask_claim",
            "state",
            "priority",
            "available_at",
            "lease_expires_at",
            "created_at",
            "id",
        ),
        Index("ix_subscriptionsearchtask_batch_position", "batch_id", "position", "id"),
        Index("ix_subscriptionsearchtask_subscription", "subscription_id", "created_at", "id"),
    )


class SubscriptionSiteBudget(Base):
    """记录兜底搜索对单个站点的唯一租约与错误冷却。"""

    id = get_id_column()
    site_id: Mapped[int] = mapped_column(Integer, nullable=False)
    lease_owner: Mapped[Optional[str]] = mapped_column(String(128))
    lease_token: Mapped[Optional[str]] = mapped_column(String(64))
    lease_expires_at: Mapped[Optional[str]] = mapped_column(String(40))
    next_allowed_at: Mapped[str] = mapped_column(String(40), nullable=False)
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    success_streak: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_outcome: Mapped[Optional[str]] = mapped_column(String(32))
    last_error: Mapped[Optional[str]] = mapped_column(Text)
    updated_at: Mapped[str] = mapped_column(String(40), nullable=False)

    __table_args__ = (
        UniqueConstraint("site_id", name="uq_subscriptionsitebudget_site_id"),
        Index(
            "ix_subscriptionsitebudget_ready",
            "next_allowed_at",
            "lease_expires_at",
            "site_id",
        ),
    )
