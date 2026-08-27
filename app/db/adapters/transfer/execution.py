"""整理步骤执行与终态结算端口的 SQLAlchemy 短事务适配器。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.application.transfer.execution import (
    TransferExecutionCheckpoint,
    TransferExecutionConflictError,
    TransferExecutionLeaseLostError,
    TransferExecutionSnapshot,
    TransferExecutionState,
    TransferExecutionStep,
    TransferManualReviewDecision,
    TransferManualReviewPage,
    TransferManualReviewResult,
    TransferManualReviewSource,
    TransferManualReviewStepView,
    TransferManualReviewTaskView,
    TransferRetryRequestResult,
    TransferStepIntent,
    TransferStepResult,
    TransferStepState,
)
from app.db.models.transferexecutionstep import (
    TransferExecutionStep as TransferExecutionStepModel,
)
from app.db.models.transferpending import TransferPending
from app.db.oper.transferexecutionstep import TransferExecutionStepOper
from app.db.oper.transferpending import TransferPendingOper
from app.db.uow import SqlAlchemyUnitOfWork

_LEGACY_REVIEW_STEP_KIND = "legacy_execution_review"


def _local_now() -> datetime:
    """返回业务审计字段使用的宿主本地时间。"""
    return datetime.now()


def _utc_now() -> datetime:
    """返回租约 fencing 使用的 UTC 时间。"""
    return datetime.now(timezone.utc)


class TransactionalTransferExecutionRepository:
    """以操作级短 Session 实现整理执行持久化端口。"""

    def __init__(
            self,
            session_factory: Callable[[], Session],
            *,
            local_clock: Callable[[], datetime] = _local_now,
            lease_clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        """保存 Session 工厂及可测试时钟，不持有跨外部 I/O 事务。"""
        self._session_factory = session_factory
        self._local_clock = local_clock
        self._lease_clock = lease_clock

    @staticmethod
    def _format_local(value: datetime) -> str:
        """编码与既有业务审计列一致的本地时间。"""
        return value.strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def _format_utc(value: datetime) -> str:
        """编码可按字典序比较的固定宽度 UTC 租约时间。"""
        return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")

    @staticmethod
    def _execution_steps(
            steps: list[TransferExecutionStepModel],
    ) -> list[TransferExecutionStepModel]:
        """排除只用于迁移人工审计、不得参与真实执行的 synthetic 步骤。"""
        return [step for step in steps if step.kind != _LEGACY_REVIEW_STEP_KIND]

    @staticmethod
    def _project_step(step: TransferExecutionStepModel) -> TransferExecutionStep:
        """在 Session 有效期内冻结 ORM 步骤为 Application DTO。"""
        if (step.result_version is None) != (step.result_payload is None):
            raise TransferExecutionConflictError("整理步骤结果版本与 payload 不完整")
        result = (
            TransferStepResult(
                version=step.result_version,
                payload=dict(step.result_payload),
            )
            if step.result_version is not None and step.result_payload is not None
            else None
        )
        intent = TransferStepIntent(
            operation_id=step.operation_id,
            checkpoint_fingerprint=step.checkpoint_fingerprint,
            ordinal=step.ordinal,
            phase=step.phase,
            kind=step.kind,
            payload=dict(step.intent_payload),
            version=step.intent_version,
        )
        return TransferExecutionStep(
            task_id=step.task_id,
            operation_id=step.operation_id,
            checkpoint_fingerprint=step.checkpoint_fingerprint,
            ordinal=step.ordinal,
            phase=step.phase,
            kind=step.kind,
            state=TransferStepState(step.state),
            attempt_token=step.attempt_token,
            attempt_count=step.attempt_count,
            intent=intent,
            result=result,
            last_error=step.last_error,
            prepared_at=step.prepared_at,
            started_at=step.started_at,
            completed_at=step.completed_at,
            updated_at=step.updated_at,
        )

    @classmethod
    def _project_snapshot(
            cls,
            pending: TransferPending,
            steps: list[TransferExecutionStepModel],
    ) -> TransferExecutionSnapshot:
        """在 Session 有效期内冻结 pending 与步骤聚合状态。"""
        has_checkpoint_column = pending.execution_payload is not None
        has_checkpoint_identity = (
            pending.execution_version is not None
            and pending.execution_fingerprint is not None
        )
        if has_checkpoint_column != has_checkpoint_identity:
            raise TransferExecutionConflictError("整理执行检查点列不完整")
        checkpoint = None
        if pending.execution_payload is not None:
            checkpoint = TransferExecutionCheckpoint.from_payload(
                pending.execution_payload,
                fingerprint=pending.execution_fingerprint or "",
            )
            if checkpoint.version != pending.execution_version:
                raise TransferExecutionConflictError("整理执行检查点列版本不一致")
        return TransferExecutionSnapshot(
            task_id=pending.task_id,
            state=TransferExecutionState(pending.execution_state),
            checkpoint=checkpoint,
            retry_generation=pending.retry_generation,
            retry_count=pending.retry_count,
            retry_due_at=pending.retry_due_at,
            settlement_revision=pending.settlement_revision,
            terminal_history_id=pending.terminal_history_id,
            last_error=pending.last_error,
            steps=tuple(
                cls._project_step(step)
                for step in cls._execution_steps(steps)
            ),
        )

    @classmethod
    def _project_manual_review(
            cls,
            pending: TransferPending,
            steps: list[TransferExecutionStepModel],
    ) -> TransferManualReviewTaskView:
        """冻结人工复核详情，并只选择当前或最近一次复核步骤。"""
        state = TransferExecutionState(pending.execution_state)
        if state is TransferExecutionState.MANUAL_REVIEW:
            candidates = [
                step
                for step in steps
                if step.state == TransferStepState.MANUAL_REVIEW.value
            ]
        elif (
                state is TransferExecutionState.RETRY_WAIT
                and pending.manual_review_revision > 0
        ):
            candidates = [
                step
                for step in steps
                if step.state in {
                    TransferStepState.FAILED.value,
                    TransferStepState.SUCCEEDED.value,
                }
                and step.updated_at == pending.reviewed_at
            ]
        else:
            raise TransferExecutionConflictError("整理任务不属于公开人工复核状态")
        if not candidates:
            raise TransferExecutionConflictError("人工复核任务缺少对应步骤证据")
        step = max(candidates, key=lambda item: (item.ordinal, item.id))
        evidence = dict(step.result_payload) if step.result_payload is not None else None
        return TransferManualReviewTaskView(
            task_id=pending.task_id,
            source=TransferManualReviewSource(
                storage=pending.storage,
                path=pending.src_path,
            ),
            state=state,
            step=TransferManualReviewStepView(
                operation_id=step.operation_id,
                kind=step.kind,
                intent=dict(step.intent_payload),
                evidence=evidence,
                error=step.last_error,
            ),
            review_revision=pending.manual_review_revision,
        )

    @staticmethod
    def _intent_matches(
            step: TransferExecutionStepModel,
            *,
            task_id: str,
            intent: TransferStepIntent,
    ) -> bool:
        """判断同 operation ID 的既有步骤是否为完全相同的冻结意图。"""
        return all((
            step.task_id == task_id,
            step.checkpoint_fingerprint == intent.checkpoint_fingerprint,
            step.ordinal == intent.ordinal,
            step.phase == intent.phase,
            step.kind == intent.kind,
            step.intent_version == intent.version,
            step.intent_payload == intent.payload,
        ))

    @staticmethod
    def _raise_fenced_failure(
            pending: Optional[TransferPending],
            *,
            lease_token: str,
            now_utc: str,
            detail: str,
    ) -> None:
        """区分租约丢失与同租约内的状态或 attempt 冲突。"""
        if (
                pending is None
                or pending.lease_token != lease_token
                or pending.lease_expires_at is None
                or pending.lease_expires_at <= now_utc
        ):
            raise TransferExecutionLeaseLostError("整理任务租约已失效或被接管")
        raise TransferExecutionConflictError(detail)

    def _times(self) -> tuple[str, str]:
        """生成一次事务内共享的 UTC fencing 与本地审计时间。"""
        return (
            self._format_utc(self._lease_clock()),
            self._format_local(self._local_clock()),
        )

    @staticmethod
    def _rollback(transaction: SqlAlchemyUnitOfWork) -> None:
        """统一回滚失败的操作级事务。"""
        transaction.rollback()

    def get_snapshot(self, *, task_id: str) -> Optional[TransferExecutionSnapshot]:
        """使用一次只读短 Session 获取任务执行快照。"""
        with self._session_factory() as session:
            pending = TransferPendingOper(session).get_by_task_id(task_id=task_id)
            if pending is None:
                return None
            steps = TransferExecutionStepOper(session).list_by_task_id(task_id=task_id)
            return self._project_snapshot(pending, steps)

    def list_manual_reviews(
            self,
            *,
            state: TransferExecutionState,
            page: int,
            page_size: int,
    ) -> TransferManualReviewPage:
        """在数据库内过滤和分页人工复核任务，再冻结步骤证据。"""
        if state not in {
            TransferExecutionState.MANUAL_REVIEW,
            TransferExecutionState.RETRY_WAIT,
        }:
            raise ValueError(f"人工复核查询不支持状态：{state.value}")
        state_predicate = TransferPending.execution_state == state.value
        if state is TransferExecutionState.RETRY_WAIT:
            state_predicate = and_(
                state_predicate,
                TransferPending.manual_review_revision > 0,
            )
        with self._session_factory() as session:
            total = int(session.scalar(
                select(func.count()).select_from(TransferPending).where(state_predicate)
            ) or 0)
            pendings = list(session.scalars(
                select(TransferPending)
                .where(state_predicate)
                .order_by(TransferPending.updated_at.desc(), TransferPending.id.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            ).all())
            step_oper = TransferExecutionStepOper(session)
            items = tuple(
                self._project_manual_review(
                    pending,
                    step_oper.list_by_task_id(task_id=pending.task_id),
                )
                for pending in pendings
            )
            return TransferManualReviewPage(
                items=items,
                total=total,
                page=page,
                page_size=page_size,
            )

    def get_manual_review(
            self,
            *,
            task_id: str,
    ) -> Optional[TransferManualReviewTaskView]:
        """读取待人工复核或已判定等待恢复的单个任务。"""
        with self._session_factory() as session:
            pending = session.scalar(
                select(TransferPending).where(
                    TransferPending.task_id == task_id,
                    or_(
                        TransferPending.execution_state
                        == TransferExecutionState.MANUAL_REVIEW.value,
                        and_(
                            TransferPending.execution_state
                            == TransferExecutionState.RETRY_WAIT.value,
                            TransferPending.manual_review_revision > 0,
                        ),
                    ),
                )
            )
            if pending is None:
                return None
            return self._project_manual_review(
                pending,
                TransferExecutionStepOper(session).list_by_task_id(task_id=task_id),
            )

    def prepare_step(
            self,
            *,
            task_id: str,
            lease_token: str,
            intent: TransferStepIntent,
    ) -> TransferExecutionStep:
        """在外部副作用前以有效 lease 幂等提交稳定步骤意图。"""
        now_utc, updated_at = self._times()
        with self._session_factory() as session:
            transaction = SqlAlchemyUnitOfWork(session)
            try:
                pending_oper = TransferPendingOper(session)
                pending = pending_oper.get_by_task_id(task_id=task_id)
                updated = pending_oper.stage_execution_running(
                    task_id=task_id,
                    lease_token=lease_token,
                    now_utc=now_utc,
                    updated_at=updated_at,
                )
                if updated != 1:
                    self._raise_fenced_failure(
                        pending,
                        lease_token=lease_token,
                        now_utc=now_utc,
                        detail="整理任务当前状态不能准备外部步骤",
                    )
                oper = TransferExecutionStepOper(session)
                step = oper.get_by_operation_id(operation_id=intent.operation_id)
                if step is None:
                    step = oper.stage_prepare(
                        task_id=task_id,
                        operation_id=intent.operation_id,
                        checkpoint_fingerprint=intent.checkpoint_fingerprint,
                        ordinal=intent.ordinal,
                        phase=intent.phase,
                        kind=intent.kind,
                        intent_version=intent.version,
                        intent_payload=dict(intent.payload),
                        now_time=updated_at,
                    )
                    session.flush()
                elif not self._intent_matches(step, task_id=task_id, intent=intent):
                    raise TransferExecutionConflictError(
                        "稳定 operation ID 已绑定不同步骤意图"
                    )
                projected = self._project_step(step)
                transaction.commit()
                return projected
            except IntegrityError as error:
                self._rollback(transaction)
                raise TransferExecutionConflictError(
                    "整理步骤 operation ID 或全局序号发生并发冲突"
                ) from error
            except Exception:
                self._rollback(transaction)
                raise

    def start_step(
            self,
            *,
            task_id: str,
            lease_token: str,
            operation_id: str,
            attempt_token: str,
    ) -> TransferExecutionStep:
        """以当前 lease 把 PREPARED 步骤推进为 STARTED。"""
        now_utc, updated_at = self._times()
        with self._session_factory() as session:
            transaction = SqlAlchemyUnitOfWork(session)
            try:
                oper = TransferExecutionStepOper(session)
                updated = oper.stage_start_attempt(
                    task_id=task_id,
                    lease_token=lease_token,
                    operation_id=operation_id,
                    attempt_token=attempt_token,
                    now_utc=now_utc,
                    updated_at=updated_at,
                )
                if updated != 1:
                    self._raise_fenced_failure(
                        TransferPendingOper(session).get_by_task_id(task_id=task_id),
                        lease_token=lease_token,
                        now_utc=now_utc,
                        detail="整理步骤不是可开始的 PREPARED 状态",
                    )
                session.expire_all()
                step = oper.get_by_operation_id(operation_id=operation_id)
                if step is None:
                    raise TransferExecutionConflictError("整理步骤开始后无法回读")
                projected = self._project_step(step)
                transaction.commit()
                return projected
            except Exception:
                self._rollback(transaction)
                raise

    def restart_after_not_applied(
            self,
            *,
            task_id: str,
            lease_token: str,
            operation_id: str,
            previous_attempt_token: str,
            attempt_token: str,
            evidence: TransferStepResult,
    ) -> TransferExecutionStep:
        """以严格 NOT_APPLIED 证据和旧 attempt CAS 重启遗留步骤。"""
        now_utc, updated_at = self._times()
        with self._session_factory() as session:
            transaction = SqlAlchemyUnitOfWork(session)
            try:
                oper = TransferExecutionStepOper(session)
                updated = oper.stage_restart_after_not_applied(
                    task_id=task_id,
                    lease_token=lease_token,
                    operation_id=operation_id,
                    previous_attempt_token=previous_attempt_token,
                    attempt_token=attempt_token,
                    result_version=evidence.version,
                    result_payload=dict(evidence.payload),
                    now_utc=now_utc,
                    updated_at=updated_at,
                )
                if updated != 1:
                    self._raise_fenced_failure(
                        TransferPendingOper(session).get_by_task_id(task_id=task_id),
                        lease_token=lease_token,
                        now_utc=now_utc,
                        detail="遗留步骤状态或旧 attempt token 已变化",
                    )
                session.expire_all()
                step = oper.get_by_operation_id(operation_id=operation_id)
                if step is None:
                    raise TransferExecutionConflictError("整理步骤重启后无法回读")
                projected = self._project_step(step)
                transaction.commit()
                return projected
            except Exception:
                self._rollback(transaction)
                raise

    def resume_failed_step(
            self,
            *,
            task_id: str,
            lease_token: str,
            operation_id: str,
            attempt_token: str,
    ) -> TransferExecutionStep:
        """在 retry_wait 到期并重新 claim 后恢复同一 FAILED 操作。"""
        now_utc, updated_at = self._times()
        with self._session_factory() as session:
            transaction = SqlAlchemyUnitOfWork(session)
            try:
                pending_oper = TransferPendingOper(session)
                pending = pending_oper.get_by_task_id(task_id=task_id)
                pending_updated = pending_oper.stage_execution_running(
                    task_id=task_id,
                    lease_token=lease_token,
                    now_utc=now_utc,
                    updated_at=updated_at,
                )
                if pending_updated != 1:
                    self._raise_fenced_failure(
                        pending,
                        lease_token=lease_token,
                        now_utc=now_utc,
                        detail="重试任务未到期或当前状态不能恢复",
                    )
                oper = TransferExecutionStepOper(session)
                updated = oper.stage_resume_failed_attempt(
                    task_id=task_id,
                    lease_token=lease_token,
                    operation_id=operation_id,
                    attempt_token=attempt_token,
                    now_utc=now_utc,
                    updated_at=updated_at,
                )
                if updated != 1:
                    self._raise_fenced_failure(
                        pending,
                        lease_token=lease_token,
                        now_utc=now_utc,
                        detail="整理步骤不是可恢复的 FAILED 状态",
                    )
                session.expire_all()
                step = oper.get_by_operation_id(operation_id=operation_id)
                if step is None:
                    raise TransferExecutionConflictError("恢复重试后无法回读整理步骤")
                projected = self._project_step(step)
                transaction.commit()
                return projected
            except Exception:
                self._rollback(transaction)
                raise

    def complete_step(
            self,
            *,
            task_id: str,
            lease_token: str,
            operation_id: str,
            attempt_token: str,
            result: TransferStepResult,
    ) -> TransferExecutionStep:
        """以 lease 与 attempt 双 CAS 提交外部操作成功证据。"""
        now_utc, updated_at = self._times()
        with self._session_factory() as session:
            transaction = SqlAlchemyUnitOfWork(session)
            try:
                oper = TransferExecutionStepOper(session)
                updated = oper.stage_complete_attempt(
                    task_id=task_id,
                    lease_token=lease_token,
                    operation_id=operation_id,
                    attempt_token=attempt_token,
                    result_version=result.version,
                    result_payload=dict(result.payload),
                    now_utc=now_utc,
                    updated_at=updated_at,
                )
                if updated != 1:
                    self._raise_fenced_failure(
                        TransferPendingOper(session).get_by_task_id(task_id=task_id),
                        lease_token=lease_token,
                        now_utc=now_utc,
                        detail="整理步骤 attempt token 已变化或状态不是 STARTED",
                    )
                session.expire_all()
                step = oper.get_by_operation_id(operation_id=operation_id)
                if step is None:
                    raise TransferExecutionConflictError("整理步骤完成后无法回读")
                projected = self._project_step(step)
                transaction.commit()
                return projected
            except Exception:
                self._rollback(transaction)
                raise

    def defer_step(
            self,
            *,
            task_id: str,
            lease_token: str,
            operation_id: str,
            attempt_token: str,
            error: str,
            retry_due_at: str,
            evidence: Optional[TransferStepResult] = None,
    ) -> TransferExecutionSnapshot:
        """原子提交已知失败证据、重试世代和到期时间并释放 lease。"""
        now_utc, updated_at = self._times()
        with self._session_factory() as session:
            transaction = SqlAlchemyUnitOfWork(session)
            try:
                step_oper = TransferExecutionStepOper(session)
                updated = step_oper.stage_fail_attempt(
                    task_id=task_id,
                    lease_token=lease_token,
                    operation_id=operation_id,
                    attempt_token=attempt_token,
                    error=error,
                    result_version=evidence.version if evidence else None,
                    result_payload=dict(evidence.payload) if evidence else None,
                    now_utc=now_utc,
                    updated_at=updated_at,
                )
                pending_oper = TransferPendingOper(session)
                pending = pending_oper.get_by_task_id(task_id=task_id)
                if updated != 1:
                    self._raise_fenced_failure(
                        pending,
                        lease_token=lease_token,
                        now_utc=now_utc,
                        detail="整理步骤已知失败证据与当前 attempt 冲突",
                    )
                pending_updated = pending_oper.stage_defer_execution(
                    task_id=task_id,
                    lease_token=lease_token,
                    error=error,
                    retry_due_at=retry_due_at,
                    now_utc=now_utc,
                    updated_at=updated_at,
                )
                if pending_updated != 1:
                    self._raise_fenced_failure(
                        pending,
                        lease_token=lease_token,
                        now_utc=now_utc,
                        detail="整理任务不能进入重试等待",
                    )
                session.flush()
                session.expire_all()
                pending = pending_oper.get_by_task_id(task_id=task_id)
                if pending is None:
                    raise TransferExecutionConflictError("重试任务无法回读")
                snapshot = self._project_snapshot(
                    pending,
                    step_oper.list_by_task_id(task_id=task_id),
                )
                transaction.commit()
                return snapshot
            except Exception:
                self._rollback(transaction)
                raise

    def exhaust_step(
            self,
            *,
            task_id: str,
            lease_token: str,
            operation_id: str,
            attempt_token: str,
            error: str,
            evidence: Optional[TransferStepResult] = None,
    ) -> TransferExecutionSnapshot:
        """原子提交预算耗尽步骤失败，并保留 lease 建立失败结算检查点。"""
        now_utc, updated_at = self._times()
        with self._session_factory() as session:
            transaction = SqlAlchemyUnitOfWork(session)
            try:
                step_oper = TransferExecutionStepOper(session)
                updated = step_oper.stage_fail_attempt(
                    task_id=task_id,
                    lease_token=lease_token,
                    operation_id=operation_id,
                    attempt_token=attempt_token,
                    error=error,
                    result_version=evidence.version if evidence else None,
                    result_payload=dict(evidence.payload) if evidence else None,
                    now_utc=now_utc,
                    updated_at=updated_at,
                )
                pending_oper = TransferPendingOper(session)
                pending = pending_oper.get_by_task_id(task_id=task_id)
                if updated != 1:
                    self._raise_fenced_failure(
                        pending,
                        lease_token=lease_token,
                        now_utc=now_utc,
                        detail="预算耗尽失败证据与当前 attempt 冲突",
                    )
                session.flush()
                steps = self._execution_steps(
                    step_oper.list_by_task_id(task_id=task_id)
                )
                checkpoint = TransferExecutionCheckpoint.create(
                    payload={
                        "outcome": "failed",
                        "failed_operation_id": operation_id,
                        "error": error,
                        "evidence": (
                            {
                                "schema_version": evidence.version,
                                "payload": dict(evidence.payload),
                            }
                            if evidence
                            else None
                        ),
                    },
                    operation_ids=tuple(step.operation_id for step in steps),
                )
                pending_updated = pending_oper.stage_checkpoint_exhausted_failure(
                    task_id=task_id,
                    lease_token=lease_token,
                    execution_version=checkpoint.version,
                    execution_payload=checkpoint.to_payload(),
                    execution_fingerprint=checkpoint.fingerprint,
                    error=error,
                    now_utc=now_utc,
                    updated_at=updated_at,
                )
                if pending_updated != 1:
                    self._raise_fenced_failure(
                        pending,
                        lease_token=lease_token,
                        now_utc=now_utc,
                        detail="整理任务不能进入失败结算状态",
                    )
                session.flush()
                session.expire_all()
                pending = pending_oper.get_by_task_id(task_id=task_id)
                if pending is None:
                    raise TransferExecutionConflictError("失败结算任务无法回读")
                snapshot = self._project_snapshot(pending, steps)
                transaction.commit()
                return snapshot
            except Exception:
                self._rollback(transaction)
                raise

    def request_retry(
            self,
            *,
            task_id: str,
            reason: str,
            requested_by: str,
    ) -> TransferRetryRequestResult:
        """以短事务登记用户重试意图，不 claim、执行或删除既有证据。"""
        retry_due_at, updated_at = self._times()
        with self._session_factory() as session:
            transaction = SqlAlchemyUnitOfWork(session)
            try:
                oper = TransferPendingOper(session)
                pending = oper.get_by_task_id(task_id=task_id)
                if pending is None:
                    raise TransferExecutionConflictError("未找到可重试的整理任务")
                state = TransferExecutionState(pending.execution_state)
                if state is TransferExecutionState.RETRY_WAIT:
                    return TransferRetryRequestResult(
                        accepted=True,
                        state=state,
                        retry_generation=pending.retry_generation,
                        message="整理任务已在等待重试",
                    )
                if state is not TransferExecutionState.FAILED:
                    message = (
                        "人工复核任务必须先完成专门判定"
                        if state is TransferExecutionState.MANUAL_REVIEW
                        else "整理任务当前状态不接受用户重试"
                    )
                    return TransferRetryRequestResult(
                        accepted=False,
                        state=state,
                        retry_generation=pending.retry_generation,
                        message=message,
                    )
                updated = oper.stage_request_execution_retry(
                    task_id=task_id,
                    reason=reason,
                    requested_by=requested_by,
                    retry_due_at=retry_due_at,
                    updated_at=updated_at,
                )
                if updated != 1:
                    session.expire_all()
                    pending = oper.get_by_task_id(task_id=task_id)
                    if pending is None:
                        raise TransferExecutionConflictError("重试请求竞争后任务已不存在")
                    state = TransferExecutionState(pending.execution_state)
                    if state is TransferExecutionState.RETRY_WAIT:
                        return TransferRetryRequestResult(
                            accepted=True,
                            state=state,
                            retry_generation=pending.retry_generation,
                            message="整理任务已由并发请求登记重试",
                        )
                    return TransferRetryRequestResult(
                        accepted=False,
                        state=state,
                        retry_generation=pending.retry_generation,
                        message="整理任务状态已变化，未登记重试",
                    )
                session.flush()
                session.expire_all()
                pending = oper.get_by_task_id(task_id=task_id)
                if pending is None:
                    raise TransferExecutionConflictError("登记重试后任务无法回读")
                result = TransferRetryRequestResult(
                    accepted=True,
                    state=TransferExecutionState.RETRY_WAIT,
                    retry_generation=pending.retry_generation,
                    message="整理任务已登记重试",
                )
                transaction.commit()
                return result
            except Exception:
                self._rollback(transaction)
                raise

    def resolve_manual_review(
            self,
            *,
            task_id: str,
            operation_id: str,
            decision: TransferManualReviewDecision,
            actor: str,
            reason: str,
            result: Optional[TransferStepResult] = None,
    ) -> TransferManualReviewResult:
        """在无 lease 短事务中原子提交步骤与 pending 的人工判定。"""
        if decision is TransferManualReviewDecision.FAILED:
            raise TransferExecutionConflictError(
                "人工失败终态尚不能绕过 lease durable 结算"
            )
        if decision is TransferManualReviewDecision.APPLIED and result is None:
            raise ValueError("人工判定已发生时必须提供结果证据")
        retry_due_at, updated_at = self._times()
        target_state = (
            TransferStepState.SUCCEEDED.value
            if decision is TransferManualReviewDecision.APPLIED
            else TransferStepState.FAILED.value
        )
        with self._session_factory() as session:
            transaction = SqlAlchemyUnitOfWork(session)
            try:
                step_oper = TransferExecutionStepOper(session)
                step = step_oper.get_by_operation_id(operation_id=operation_id)
                if (
                        decision is TransferManualReviewDecision.APPLIED
                        and step is not None
                        and step.task_id == task_id
                        and step.kind == _LEGACY_REVIEW_STEP_KIND
                ):
                    raise TransferExecutionConflictError(
                        "升级遗留步骤没有足够证据证明外部操作已发生；"
                        "请先人工回滚，或确认未发生后选择 NOT_APPLIED"
                    )
                step_updated = step_oper.stage_resolve_manual_review(
                    task_id=task_id,
                    operation_id=operation_id,
                    target_state=target_state,
                    reason=reason,
                    result_version=result.version if result else None,
                    result_payload=dict(result.payload) if result else None,
                    updated_at=updated_at,
                )
                if step_updated != 1:
                    raise TransferExecutionConflictError(
                        "步骤已不处于可判定的无租约人工复核态"
                    )
                pending_oper = TransferPendingOper(session)
                pending_updated = pending_oper.stage_resolve_manual_review(
                    task_id=task_id,
                    decision=decision.value,
                    actor=actor,
                    reason=reason,
                    retry_due_at=retry_due_at,
                    updated_at=updated_at,
                )
                if pending_updated != 1:
                    raise TransferExecutionConflictError(
                        "任务已不处于可判定的无租约人工复核态"
                    )
                session.flush()
                session.expire_all()
                pending = pending_oper.get_by_task_id(task_id=task_id)
                step = step_oper.get_by_operation_id(operation_id=operation_id)
                if pending is None or step is None:
                    raise TransferExecutionConflictError("人工判定提交后无法回读")
                resolved = TransferManualReviewResult(
                    task_id=task_id,
                    operation_id=operation_id,
                    decision=decision,
                    state=TransferExecutionState(pending.execution_state),
                    review_revision=pending.manual_review_revision,
                    step=self._project_step(step),
                )
                transaction.commit()
                return resolved
            except Exception:
                self._rollback(transaction)
                raise

    def mark_manual_review(
            self,
            *,
            task_id: str,
            lease_token: str,
            operation_id: str,
            error: str,
            evidence: Optional[TransferStepResult] = None,
            attempt_token: Optional[str] = None,
    ) -> TransferExecutionSnapshot:
        """原子隔离外部结果未知的步骤与任务，并释放自动调度 lease。"""
        now_utc, updated_at = self._times()
        with self._session_factory() as session:
            transaction = SqlAlchemyUnitOfWork(session)
            try:
                step_oper = TransferExecutionStepOper(session)
                updated = step_oper.stage_manual_review(
                    task_id=task_id,
                    lease_token=lease_token,
                    operation_id=operation_id,
                    attempt_token=attempt_token,
                    error=error,
                    result_version=evidence.version if evidence else None,
                    result_payload=dict(evidence.payload) if evidence else None,
                    now_utc=now_utc,
                    updated_at=updated_at,
                )
                pending_oper = TransferPendingOper(session)
                pending = pending_oper.get_by_task_id(task_id=task_id)
                if updated != 1:
                    self._raise_fenced_failure(
                        pending,
                        lease_token=lease_token,
                        now_utc=now_utc,
                        detail="人工复核证据与当前步骤状态冲突",
                    )
                pending_updated = pending_oper.stage_mark_execution_manual_review(
                    task_id=task_id,
                    lease_token=lease_token,
                    error=error,
                    now_utc=now_utc,
                    updated_at=updated_at,
                )
                if pending_updated != 1:
                    self._raise_fenced_failure(
                        pending,
                        lease_token=lease_token,
                        now_utc=now_utc,
                        detail="整理任务不能进入人工复核",
                    )
                session.flush()
                session.expire_all()
                pending = pending_oper.get_by_task_id(task_id=task_id)
                if pending is None:
                    raise TransferExecutionConflictError("人工复核任务无法回读")
                snapshot = self._project_snapshot(
                    pending,
                    step_oper.list_by_task_id(task_id=task_id),
                )
                transaction.commit()
                return snapshot
            except Exception:
                self._rollback(transaction)
                raise

    def checkpoint_execution(
            self,
            *,
            task_id: str,
            lease_token: str,
            checkpoint: TransferExecutionCheckpoint,
    ) -> TransferExecutionSnapshot:
        """仅在完整步骤集合均成功时提交可重放终态执行检查点。"""
        now_utc, updated_at = self._times()
        with self._session_factory() as session:
            transaction = SqlAlchemyUnitOfWork(session)
            try:
                step_oper = TransferExecutionStepOper(session)
                steps = self._execution_steps(
                    step_oper.list_by_task_id(task_id=task_id)
                )
                step_ids = {step.operation_id for step in steps}
                if step_ids != set(checkpoint.operation_ids):
                    raise TransferExecutionConflictError(
                        "执行检查点引用的步骤集合与持久步骤不一致"
                    )
                if any(step.state != TransferStepState.SUCCEEDED.value for step in steps):
                    raise TransferExecutionConflictError(
                        "存在未成功步骤，不能提交执行检查点"
                    )
                pending_oper = TransferPendingOper(session)
                pending = pending_oper.get_by_task_id(task_id=task_id)
                running = pending_oper.stage_execution_running(
                    task_id=task_id,
                    lease_token=lease_token,
                    now_utc=now_utc,
                    updated_at=updated_at,
                )
                if running != 1:
                    self._raise_fenced_failure(
                        pending,
                        lease_token=lease_token,
                        now_utc=now_utc,
                        detail="整理任务当前状态不能建立执行检查点",
                    )
                updated = pending_oper.stage_checkpoint_execution(
                    task_id=task_id,
                    lease_token=lease_token,
                    execution_version=checkpoint.version,
                    execution_payload=checkpoint.to_payload(),
                    execution_fingerprint=checkpoint.fingerprint,
                    now_utc=now_utc,
                    updated_at=updated_at,
                )
                if updated != 1:
                    self._raise_fenced_failure(
                        pending,
                        lease_token=lease_token,
                        now_utc=now_utc,
                        detail="整理任务当前状态不能提交执行检查点",
                    )
                session.flush()
                session.expire_all()
                pending = pending_oper.get_by_task_id(task_id=task_id)
                if pending is None:
                    raise TransferExecutionConflictError("执行检查点任务无法回读")
                snapshot = self._project_snapshot(pending, steps)
                transaction.commit()
                return snapshot
            except Exception:
                self._rollback(transaction)
                raise
