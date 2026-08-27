"""整理外部操作步骤的显式 Session 数据访问对象。"""

from __future__ import annotations

from typing import Any, Optional

from sqlalchemy.orm import Session

from app.db.base import DbOper
from app.db.models.transferexecutionstep import TransferExecutionStep


class TransferExecutionStepOper(DbOper):
    """在调用方事务中查询或暂存整理外部操作步骤。"""

    def _session(self) -> Session:
        """返回调用方同步 Session，拒绝隐式事务破坏原子状态推进。"""
        if not isinstance(self._db, Session):
            raise RuntimeError("整理执行步骤写入需要调用方提供同步 Session")
        return self._db

    def get_by_operation_id(
            self,
            *,
            operation_id: str,
    ) -> Optional[TransferExecutionStep]:
        """按稳定操作标识查询步骤。"""
        return TransferExecutionStep.get_by_operation_id(
            self._session(),
            operation_id=operation_id,
        )

    def list_by_task_id(self, *, task_id: str) -> list[TransferExecutionStep]:
        """按全局序号查询任务的全部步骤。"""
        return TransferExecutionStep.list_by_task_id(
            self._session(),
            task_id=task_id,
        )

    def stage_prepare(
            self,
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
    ) -> TransferExecutionStep:
        """暂存尚未执行的稳定步骤意图。"""
        return TransferExecutionStep.stage_prepare(
            self._session(),
            task_id=task_id,
            operation_id=operation_id,
            checkpoint_fingerprint=checkpoint_fingerprint,
            ordinal=ordinal,
            phase=phase,
            kind=kind,
            intent_version=intent_version,
            intent_payload=intent_payload,
            now_time=now_time,
        )

    def stage_start_attempt(
            self,
            *,
            task_id: str,
            lease_token: str,
            operation_id: str,
            attempt_token: str,
            now_utc: str,
            updated_at: str,
    ) -> int:
        """以有效任务租约暂存新步骤尝试。"""
        return TransferExecutionStep.start_attempt(
            self._session(),
            task_id=task_id,
            lease_token=lease_token,
            operation_id=operation_id,
            attempt_token=attempt_token,
            now_utc=now_utc,
            updated_at=updated_at,
        )

    def stage_restart_after_not_applied(
            self,
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
        """以严格未发生证据暂存遗留 STARTED 步骤的安全重启。"""
        return TransferExecutionStep.restart_after_not_applied(
            self._session(),
            task_id=task_id,
            lease_token=lease_token,
            operation_id=operation_id,
            previous_attempt_token=previous_attempt_token,
            attempt_token=attempt_token,
            result_version=result_version,
            result_payload=result_payload,
            now_utc=now_utc,
            updated_at=updated_at,
        )

    def stage_resume_failed_attempt(
            self,
            *,
            task_id: str,
            lease_token: str,
            operation_id: str,
            attempt_token: str,
            now_utc: str,
            updated_at: str,
    ) -> int:
        """以重试调度的新 lease 暂存 FAILED 步骤恢复。"""
        return TransferExecutionStep.resume_failed_attempt(
            self._session(),
            task_id=task_id,
            lease_token=lease_token,
            operation_id=operation_id,
            attempt_token=attempt_token,
            now_utc=now_utc,
            updated_at=updated_at,
        )

    def stage_complete_attempt(
            self,
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
        """以 lease 与 attempt 双 CAS 暂存成功证据。"""
        return TransferExecutionStep.complete_attempt(
            self._session(),
            task_id=task_id,
            lease_token=lease_token,
            operation_id=operation_id,
            attempt_token=attempt_token,
            result_version=result_version,
            result_payload=result_payload,
            now_utc=now_utc,
            updated_at=updated_at,
        )

    def stage_fail_attempt(
            self,
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
        """以 lease 与 attempt 双 CAS 暂存已知失败证据。"""
        return TransferExecutionStep.fail_attempt(
            self._session(),
            task_id=task_id,
            lease_token=lease_token,
            operation_id=operation_id,
            attempt_token=attempt_token,
            error=error,
            result_version=result_version,
            result_payload=result_payload,
            now_utc=now_utc,
            updated_at=updated_at,
        )

    def stage_manual_review(
            self,
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
        """以当前尝试身份暂存人工复核证据。"""
        return TransferExecutionStep.mark_manual_review(
            self._session(),
            task_id=task_id,
            lease_token=lease_token,
            operation_id=operation_id,
            attempt_token=attempt_token,
            error=error,
            result_version=result_version,
            result_payload=result_payload,
            now_utc=now_utc,
            updated_at=updated_at,
        )

    def stage_resolve_manual_review(
            self,
            *,
            task_id: str,
            operation_id: str,
            target_state: str,
            reason: str,
            result_version: Optional[int],
            result_payload: Optional[dict[str, Any]],
            updated_at: str,
    ) -> int:
        """在 pending 同为无租约人工态时暂存步骤判定。"""
        return TransferExecutionStep.resolve_manual_review(
            self._session(),
            task_id=task_id,
            operation_id=operation_id,
            target_state=target_state,
            reason=reason,
            result_version=result_version,
            result_payload=result_payload,
            updated_at=updated_at,
        )

    def stage_delete_task(self, *, task_id: str) -> int:
        """暂存任务全部步骤删除。"""
        return TransferExecutionStep.delete_by_task_id(
            self._session(),
            task_id=task_id,
        )
