"""整理任务终态结算回执模型。"""

from __future__ import annotations

from typing import Optional, cast

from sqlalchemy import Boolean, Index, Integer, String, Text, UniqueConstraint, desc, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.db.base import Base, get_id_column


class TransferSettlementReceipt(Base):
    """按任务保存独立于最新历史投影的 durable 终态证据。"""

    id = get_id_column()
    task_id: Mapped[str] = mapped_column(String(64), nullable=False)
    history_id: Mapped[int] = mapped_column(Integer, nullable=False)
    settlement_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    execution_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    lease_token: Mapped[str] = mapped_column(String(64), nullable=False)
    history_status: Mapped[bool] = mapped_column(Boolean, nullable=False)
    src: Mapped[Optional[str]] = mapped_column(String)
    src_storage: Mapped[Optional[str]] = mapped_column(String)
    pending_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False)
    error: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(40), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "task_id",
            "settlement_revision",
            name="uq_transfersettlementreceipt_task_revision",
        ),
        Index(
            "ix_transfersettlementreceipt_task_revision",
            "task_id",
            "settlement_revision",
        ),
        Index(
            "ix_transfersettlementreceipt_history_id",
            "history_id",
        ),
    )

    @classmethod
    def get_latest_by_task_id(
            cls,
            db: Session,
            *,
            task_id: str,
    ) -> Optional["TransferSettlementReceipt"]:
        """按稳定任务标识读取最新已提交结算回执。"""
        if not task_id:
            return None
        return cast(
            Optional["TransferSettlementReceipt"],
            db.execute(
                select(cls)
                .where(cls.task_id == task_id)
                .order_by(desc(cls.settlement_revision))
            ).scalars().first(),
        )

    @classmethod
    def get_by_identity(
            cls,
            db: Session,
            *,
            task_id: str,
            execution_fingerprint: str,
            lease_token: str,
            outcome: str,
    ) -> Optional["TransferSettlementReceipt"]:
        """按原始执行身份读取不可变结算回执。"""
        if not all((task_id, execution_fingerprint, lease_token, outcome)):
            return None
        return cast(
            Optional["TransferSettlementReceipt"],
            db.execute(
                select(cls).where(
                    cls.task_id == task_id,
                    cls.execution_fingerprint == execution_fingerprint,
                    cls.lease_token == lease_token,
                    cls.outcome == outcome,
                ).order_by(desc(cls.settlement_revision))
            ).scalars().first(),
        )

    @classmethod
    def stage_append(
            cls,
            db: Session,
            *,
            task_id: str,
            history_id: int,
            settlement_revision: int,
            outcome: str,
            execution_fingerprint: str,
            lease_token: str,
            history_status: bool,
            src: Optional[str],
            src_storage: Optional[str],
            pending_deleted: bool,
            error: Optional[str],
            settled_at: str,
    ) -> "TransferSettlementReceipt":
        """按连续修订追加任务回执，旧修订证据永不覆盖。"""
        if not all((task_id, history_id, settlement_revision, outcome,
                    execution_fingerprint, lease_token, settled_at)):
            raise ValueError("整理结算回执缺少稳定身份或结果证据")
        if outcome not in {"succeeded", "failed"}:
            raise ValueError(f"不支持的整理结算结果：{outcome}")
        if cls.get_by_identity(
                db,
                task_id=task_id,
                execution_fingerprint=execution_fingerprint,
                lease_token=lease_token,
                outcome=outcome,
        ) is not None:
            raise ValueError("同一整理执行身份不能追加多个结算修订")
        latest = cls.get_latest_by_task_id(db, task_id=task_id)
        if latest is None:
            if settlement_revision != 1:
                raise ValueError("整理结算回执必须从修订 1 开始")
        elif settlement_revision != latest.settlement_revision + 1:
            raise ValueError("整理结算回执修订必须连续递增")
        receipt = cls(
            task_id=task_id,
            history_id=history_id,
            settlement_revision=settlement_revision,
            outcome=outcome,
            execution_fingerprint=execution_fingerprint,
            lease_token=lease_token,
            history_status=history_status,
            src=src,
            src_storage=src_storage,
            pending_deleted=pending_deleted,
            error=error,
            created_at=settled_at,
            updated_at=settled_at,
        )
        db.add(receipt)
        db.flush()
        return receipt
