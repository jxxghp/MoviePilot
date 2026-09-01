"""订阅下载提交幂等账本模型。"""

from typing import Optional

from sqlalchemy import Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, get_id_column


class SubscriptionDownloadSubmission(Base):
    """记录订阅下载唯一提交权、下载器接受事实和待对账终态。"""

    id = get_id_column()
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)
    subscription_id: Mapped[int] = mapped_column(Integer, nullable=False)
    task_id: Mapped[Optional[str]] = mapped_column(String(64))
    logical_identity: Mapped[str] = mapped_column(Text, nullable=False)
    resource_key: Mapped[str] = mapped_column(Text, nullable=False)
    coverage: Mapped[str] = mapped_column(Text, nullable=False)
    mode: Mapped[str] = mapped_column(String(32), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    attempt_token: Mapped[Optional[str]] = mapped_column(String(64))
    downloader: Mapped[Optional[str]] = mapped_column(String(128))
    download_hash: Mapped[Optional[str]] = mapped_column(String(256))
    available_at: Mapped[Optional[str]] = mapped_column(String(40))
    last_error: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(40), nullable=False)
    started_at: Mapped[Optional[str]] = mapped_column(String(40))
    finished_at: Mapped[Optional[str]] = mapped_column(String(40))

    __table_args__ = (
        UniqueConstraint(
            "idempotency_key",
            name="uq_subscriptiondownloadsubmission_idempotency_key",
        ),
        Index(
            "ix_subscriptiondownloadsubmission_task_state",
            "task_id",
            "state",
            "id",
        ),
        Index(
            "ix_subscriptiondownloadsubmission_subscription_state",
            "subscription_id",
            "state",
            "updated_at",
            "id",
        ),
    )
