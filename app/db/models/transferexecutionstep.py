"""整理任务外部操作步骤的持久化模型。"""

from __future__ import annotations

from typing import Any, Optional, cast

from sqlalchemy import (
    JSON,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    and_,
    delete,
    exists,
    or_,
    select,
    update,
)
from sqlalchemy.orm import Mapped, Session, mapped_column
from sqlalchemy.sql.selectable import Exists

from app.db.base import Base, execute_dml, get_id_column
from app.db.models.transferpending import TransferPending


class TransferExecutionStep(Base):
    """保存一次稳定外部操作的意图、尝试身份与结果证据。"""

    id = get_id_column()
    task_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("transferpending.task_id", ondelete="CASCADE"),
        nullable=False,
    )
    operation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    checkpoint_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    phase: Mapped[str] = mapped_column(String(32), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="prepared")
    attempt_token: Mapped[Optional[str]] = mapped_column(String(64))
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    intent_version: Mapped[int] = mapped_column(Integer, nullable=False)
    intent_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    result_version: Mapped[Optional[int]] = mapped_column(Integer)
    result_payload: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)
    last_error: Mapped[Optional[str]] = mapped_column(Text)
    prepared_at: Mapped[str] = mapped_column(String(40), nullable=False)
    started_at: Mapped[Optional[str]] = mapped_column(String(40))
    completed_at: Mapped[Optional[str]] = mapped_column(String(40))
    updated_at: Mapped[str] = mapped_column(String(40), nullable=False)

    __table_args__ = (
        UniqueConstraint("operation_id", name="uq_transferexecutionstep_operation_id"),
        UniqueConstraint(
            "task_id",
            "ordinal",
            name="uq_transferexecutionstep_task_ordinal",
        ),
        Index(
            "ix_transferexecutionstep_task_state_ordinal",
            "task_id",
            "state",
            "ordinal",
        ),
    )

    @classmethod
    def get_by_operation_id(
            cls,
            db: Session,
            *,
            operation_id: str,
    ) -> Optional["TransferExecutionStep"]:
        """按稳定操作标识读取步骤。"""
        if not operation_id:
            return None
        return cast(
            Optional["TransferExecutionStep"],
            db.execute(
                select(cls).where(cls.operation_id == operation_id)
            ).scalars().first(),
        )

    @classmethod
    def list_by_task_id(
            cls,
            db: Session,
            *,
            task_id: str,
    ) -> list["TransferExecutionStep"]:
        """按全局序号读取任务的全部外部操作步骤。"""
        if not task_id:
            return []
        return list(
            db.execute(
                select(cls)
                .where(cls.task_id == task_id)
                .order_by(cls.ordinal.asc())
            ).scalars().all()
        )

    @classmethod
    def stage_prepare(
            cls,
            db: Session,
            *,
            task_id: str,
            operation_id: str,
            checkpoint_fingerprint: str,
            ordinal: int,
            phase: str,
            kind: str,
            intent_version: int,
            intent_payload: dict[str, Any],
            now_time: str,
    ) -> "TransferExecutionStep":
        """在调用方事务中暂存尚未执行的稳定步骤意图。"""
        step = cls(
            task_id=task_id,
            operation_id=operation_id,
            checkpoint_fingerprint=checkpoint_fingerprint,
            ordinal=ordinal,
            phase=phase,
            kind=kind,
            state="prepared",
            attempt_count=0,
            intent_version=intent_version,
            intent_payload=intent_payload,
            prepared_at=now_time,
            updated_at=now_time,
        )
        db.add(step)
        return step

    @classmethod
    def start_attempt(
            cls,
            db: Session,
            *,
            task_id: str,
            lease_token: str,
            operation_id: str,
            attempt_token: str,
            now_utc: str,
            updated_at: str,
    ) -> int:
        """以有效任务租约 CAS 开始一次新的步骤尝试。"""
        return execute_dml(
            db,
            update(cls)
            .where(
                cls.task_id == task_id,
                cls.operation_id == operation_id,
                cls.state == "prepared",
                cls.attempt_token.is_(None),
                cls._active_lease_exists(
                    task_id=task_id,
                    lease_token=lease_token,
                    now_utc=now_utc,
                ),
            )
            .values(
                state="started",
                attempt_token=attempt_token,
                attempt_count=cls.attempt_count + 1,
                started_at=updated_at,
                completed_at=None,
                last_error=None,
                updated_at=updated_at,
            ),
            execution_options={"synchronize_session": False},
        )

    @classmethod
    def restart_after_not_applied(
            cls,
            db: Session,
            *,
            task_id: str,
            lease_token: str,
            operation_id: str,
            previous_attempt_token: str,
            attempt_token: str,
            result_version: int,
            result_payload: dict[str, Any],
            now_utc: str,
            updated_at: str,
    ) -> int:
        """以 NOT_APPLIED 证据和旧 attempt token CAS 重启遗留步骤。"""
        return execute_dml(
            db,
            update(cls)
            .where(
                cls.task_id == task_id,
                cls.operation_id == operation_id,
                cls.state == "started",
                cls.attempt_token == previous_attempt_token,
                cls._active_lease_exists(
                    task_id=task_id,
                    lease_token=lease_token,
                    now_utc=now_utc,
                ),
            )
            .values(
                attempt_token=attempt_token,
                attempt_count=cls.attempt_count + 1,
                result_version=result_version,
                result_payload=result_payload,
                started_at=updated_at,
                completed_at=None,
                last_error=None,
                updated_at=updated_at,
            ),
            execution_options={"synchronize_session": False},
        )

    @classmethod
    def resume_failed_attempt(
            cls,
            db: Session,
            *,
            task_id: str,
            lease_token: str,
            operation_id: str,
            attempt_token: str,
            now_utc: str,
            updated_at: str,
    ) -> int:
        """以重试调度的新 lease CAS 恢复 FAILED 步骤并保留失败证据。"""
        return execute_dml(
            db,
            update(cls)
            .where(
                cls.task_id == task_id,
                cls.operation_id == operation_id,
                cls.state == "failed",
                cls.attempt_token.is_(None),
                cls._active_lease_exists(
                    task_id=task_id,
                    lease_token=lease_token,
                    now_utc=now_utc,
                ),
            )
            .values(
                state="started",
                attempt_token=attempt_token,
                attempt_count=cls.attempt_count + 1,
                started_at=updated_at,
                completed_at=None,
                updated_at=updated_at,
            ),
            execution_options={"synchronize_session": False},
        )

    @classmethod
    def complete_attempt(
            cls,
            db: Session,
            *,
            task_id: str,
            lease_token: str,
            operation_id: str,
            attempt_token: str,
            result_version: int,
            result_payload: dict[str, Any],
            now_utc: str,
            updated_at: str,
    ) -> int:
        """以租约与 attempt 双 CAS 提交步骤成功证据。"""
        return execute_dml(
            db,
            update(cls)
            .where(
                cls.task_id == task_id,
                cls.operation_id == operation_id,
                cls.state == "started",
                cls.attempt_token == attempt_token,
                cls._active_lease_exists(
                    task_id=task_id,
                    lease_token=lease_token,
                    now_utc=now_utc,
                ),
            )
            .values(
                state="succeeded",
                attempt_token=None,
                result_version=result_version,
                result_payload=result_payload,
                last_error=None,
                completed_at=updated_at,
                updated_at=updated_at,
            ),
            execution_options={"synchronize_session": False},
        )

    @classmethod
    def fail_attempt(
            cls,
            db: Session,
            *,
            task_id: str,
            lease_token: str,
            operation_id: str,
            attempt_token: str,
            error: str,
            result_version: Optional[int],
            result_payload: Optional[dict[str, Any]],
            now_utc: str,
            updated_at: str,
    ) -> int:
        """以租约与 attempt 双 CAS 提交已知失败证据。"""
        return execute_dml(
            db,
            update(cls)
            .where(
                cls.task_id == task_id,
                cls.operation_id == operation_id,
                cls.state == "started",
                cls.attempt_token == attempt_token,
                cls._active_lease_exists(
                    task_id=task_id,
                    lease_token=lease_token,
                    now_utc=now_utc,
                ),
            )
            .values(
                state="failed",
                attempt_token=None,
                result_version=result_version,
                result_payload=result_payload,
                last_error=error,
                completed_at=updated_at,
                updated_at=updated_at,
            ),
            execution_options={"synchronize_session": False},
        )

    @classmethod
    def mark_manual_review(
            cls,
            db: Session,
            *,
            task_id: str,
            lease_token: str,
            operation_id: str,
            attempt_token: Optional[str],
            error: str,
            result_version: Optional[int],
            result_payload: Optional[dict[str, Any]],
            now_utc: str,
            updated_at: str,
    ) -> int:
        """以当前尝试身份隔离外部结果不可判定的步骤。"""
        attempt_match = (
            cls.attempt_token == attempt_token
            if attempt_token is not None
            else cls.attempt_token.is_(None)
        )
        return execute_dml(
            db,
            update(cls)
            .where(
                cls.task_id == task_id,
                cls.operation_id == operation_id,
                cls.state.in_(("prepared", "started", "failed")),
                attempt_match,
                cls._active_lease_exists(
                    task_id=task_id,
                    lease_token=lease_token,
                    now_utc=now_utc,
                ),
            )
            .values(
                state="manual_review",
                attempt_token=None,
                result_version=result_version,
                result_payload=result_payload,
                last_error=error,
                completed_at=updated_at,
                updated_at=updated_at,
            ),
            execution_options={"synchronize_session": False},
        )

    @classmethod
    def resolve_manual_review(
            cls,
            db: Session,
            *,
            task_id: str,
            operation_id: str,
            target_state: str,
            reason: str,
            result_version: Optional[int],
            result_payload: Optional[dict[str, Any]],
            updated_at: str,
    ) -> int:
        """仅在 pending 同为无租约人工态时 CAS 提交步骤判定。"""
        if target_state not in {"failed", "succeeded"}:
            return 0
        return execute_dml(
            db,
            update(cls)
            .where(
                cls.task_id == task_id,
                cls.operation_id == operation_id,
                cls.state == "manual_review",
                cls.attempt_token.is_(None),
                cls._manual_review_pending_exists(task_id=task_id),
            )
            .values(
                state=target_state,
                result_version=result_version,
                result_payload=result_payload,
                last_error=(reason if target_state == "failed" else None),
                completed_at=updated_at,
                updated_at=updated_at,
            ),
            execution_options={"synchronize_session": False},
        )

    @classmethod
    def delete_by_task_id(cls, db: Session, *, task_id: str) -> int:
        """在终态成功结算事务中删除任务的步骤证据。"""
        if not task_id:
            return 0
        return execute_dml(
            db,
            delete(cls).where(cls.task_id == task_id),
            execution_options={"synchronize_session": False},
        )

    @staticmethod
    def _active_lease_exists(
            *,
            task_id: str,
            lease_token: str,
            now_utc: str,
    ) -> Exists:
        """构造关联 pending 行仍持有当前有效租约的 SQL 谓词。"""
        return exists(
            select(TransferPending.id).where(
                and_(
                    TransferPending.task_id == task_id,
                    TransferPending.lease_token == lease_token,
                    TransferPending.lease_expires_at.is_not(None),
                    TransferPending.lease_expires_at > now_utc,
                    or_(
                        TransferPending.execution_state == "running",
                        TransferPending.execution_state == "not_started",
                    ),
                )
            )
        )

    @staticmethod
    def _manual_review_pending_exists(*, task_id: str) -> Exists:
        """构造关联 pending 行处于无租约人工复核态的 SQL 谓词。"""
        return exists(
            select(TransferPending.id).where(
                TransferPending.task_id == task_id,
                TransferPending.execution_state == "manual_review",
                TransferPending.lease_token.is_(None),
                TransferPending.lease_owner.is_(None),
            )
        )
