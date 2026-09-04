"""整理步骤执行与终态结算端口的 SQLAlchemy 短事务适配器。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

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
    TransferFailureDiscardResult,
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
    build_transfer_operation_id,
)
from app.application.transfer.workflow import (
    TRANSFER_ADMISSION_PLANNED,
    TRANSFER_ADMISSION_PROVIDER_PENDING,
    TRANSFER_PLAN_CHECKPOINT_LEGACY_VERSION,
    TRANSFER_PLAN_CHECKPOINT_VERSION,
    TransferPlanCheckpoint,
    TransferProviderInvocationSnapshot,
    TransferProviderReference,
)
from app.db.models.transferexecutionstep import (
    TransferExecutionStep as TransferExecutionStepModel,
)
from app.db.models.transferpending import TransferPending
from app.db.oper.transferexecutionstep import TransferExecutionStepOper
from app.db.oper.transferhistory import TransferHistoryOper
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
    def _plan_identity(
            pending: TransferPending,
    ) -> tuple[TransferPlanCheckpoint, str]:
        """恢复完整冻结计划，并返回其规范 payload 的稳定指纹。"""
        if pending.state not in {
            TRANSFER_ADMISSION_PLANNED,
            TRANSFER_ADMISSION_PROVIDER_PENDING,
        }:
            raise TransferExecutionConflictError("整理任务尚未进入可执行规划状态")
        if (
                pending.checkpoint_version is None
                or pending.checkpoint_payload is None
                or pending.planned_at is None
        ):
            raise TransferExecutionConflictError("整理任务缺少完整计划检查点")
        try:
            checkpoint = TransferPlanCheckpoint.from_payload(
                pending.checkpoint_payload
            )
        except (TypeError, ValueError) as error:
            raise TransferExecutionConflictError(
                "整理任务计划检查点无法恢复"
            ) from error
        if pending.checkpoint_version != checkpoint.schema_version:
            raise TransferExecutionConflictError("整理任务计划检查点版本不一致")
        if checkpoint.planning_input.fingerprint != pending.input_fingerprint:
            raise TransferExecutionConflictError("整理任务计划与准入输入指纹不一致")
        expected_state = (
            TRANSFER_ADMISSION_PROVIDER_PENDING
            if checkpoint.is_provider_pending
            else TRANSFER_ADMISSION_PLANNED
        )
        if pending.state != expected_state:
            raise TransferExecutionConflictError("整理任务计划类型与准入状态不一致")
        return checkpoint, checkpoint.fingerprint

    @staticmethod
    def _provider_predecessor_checkpoint(
            checkpoint: TransferPlanCheckpoint,
            step: TransferExecutionStepModel,
    ) -> Optional[TransferPlanCheckpoint]:
        """重建 provider 回退前的冻结计划，证明序号零步骤的历史归属。"""
        if (
                not checkpoint.pre_execution_cleanup_completed
                or step.ordinal != 0
                or step.phase != "provider"
                or step.kind != "legacy_transfer_provider_sequence"
        ):
            return None
        providers_payload = step.intent_payload.get("providers")
        invocation_payload = step.intent_payload.get("invocation")
        if (
                not isinstance(providers_payload, list)
                or not providers_payload
                or not all(isinstance(item, dict) for item in providers_payload)
                or not isinstance(invocation_payload, dict)
        ):
            return None
        try:
            providers = tuple(
                TransferProviderReference.from_payload(item)
                for item in providers_payload
            )
            invocation = TransferProviderInvocationSnapshot.from_payload(
                invocation_payload
            )
            if invocation.fileitem != checkpoint.planning_input.source_fileitem:
                return None
            for schema_version in (
                TRANSFER_PLAN_CHECKPOINT_VERSION,
                TRANSFER_PLAN_CHECKPOINT_LEGACY_VERSION,
            ):
                predecessor = TransferPlanCheckpoint(
                    planning_input=checkpoint.planning_input,
                    target_storage="",
                    root_target_path="",
                    final_target_path="",
                    resolved_transfer_type="",
                    items=(),
                    classification_snapshot=checkpoint.classification_snapshot,
                    resolved_meta=invocation.meta,
                    resolved_meta_kind=invocation.meta_kind,
                    resolved_mediainfo=invocation.mediainfo,
                    resolved_mediainfo_kind=invocation.mediainfo_kind,
                    resolved_episodes_info=invocation.episodes_info,
                    legacy_transfer_providers=providers,
                    provider_invocation=invocation,
                    preview=invocation.preview,
                    schema_version=schema_version,
                )
                if predecessor.fingerprint == step.checkpoint_fingerprint:
                    return predecessor
        except (TypeError, ValueError):
            return None
        return None

    @staticmethod
    def _intent_belongs_to_checkpoint(
            checkpoint: TransferPlanCheckpoint,
            *,
            phase: str,
            kind: str,
            payload: dict[str, Any],
            previous_steps: list[TransferExecutionStepModel],
    ) -> bool:
        """校验步骤类型及稳定参数由冻结计划或其已提交发现证据导出。"""
        if checkpoint.rejection_error:
            return (
                phase == "planning"
                and kind == "reject"
                and payload == {"error": checkpoint.rejection_error}
            )
        if checkpoint.is_provider_pending:
            invocation = checkpoint.provider_invocation
            if invocation is None:
                return False
            return (
                phase == "provider"
                and kind == "legacy_transfer_provider_sequence"
                and payload == {
                    "providers": [
                        provider.to_payload()
                        for provider in checkpoint.legacy_transfer_providers
                    ],
                    "invocation": invocation.to_payload(),
                }
            )
        plan_items = tuple(checkpoint.items)
        if phase == "transfer" and kind == checkpoint.resolved_transfer_type:
            return any(payload == {
                "source": item.source_fileitem.get("path"),
                "target": item.target_path,
            } for item in plan_items)
        if phase == "transfer" and kind == "materialize_target":
            return any(payload == {
                "source": item.source_fileitem,
                "target_storage": item.target_storage,
                "target_path": item.target_path,
                "transfer_type": (
                    "copy"
                    if (
                        checkpoint.resolved_transfer_type == "move"
                        and item.source_fileitem.get("storage")
                        != item.target_storage
                    )
                    else checkpoint.resolved_transfer_type
                ),
            } for item in plan_items)
        if phase == "transfer" and kind == "delete_move_source":
            return (
                checkpoint.resolved_transfer_type == "move"
                and any(
                    item.source_fileitem.get("storage") != item.target_storage
                    and payload == {
                        "source": item.source_fileitem,
                        "target_storage": item.target_storage,
                        "target_path": item.target_path,
                    }
                    for item in plan_items
                )
            )
        source = checkpoint.planning_input.source_fileitem
        if phase == "prepare" and kind == "cleanup_previous_destination":
            return payload == {"source_path": source.get("path")}
        if phase == "prepare" and kind == "ensure_target_directory":
            target_path = Path(checkpoint.final_target_path)
            directory_path = (
                target_path
                if source.get("type") == "dir"
                else target_path.parent
            )
            return payload == {
                "storage": checkpoint.target_storage,
                "path": directory_path.as_posix(),
            }
        if phase == "prepare" and kind == "delete_overwrite_target":
            return payload == {
                "storage": checkpoint.target_storage,
                "path": checkpoint.final_target_path,
            }
        if phase == "decision" and kind == "resolve_overwrite":
            return payload == {
                "source": source,
                "target_storage": checkpoint.target_storage,
                "target_path": checkpoint.final_target_path,
                "transfer_type": checkpoint.resolved_transfer_type,
                "overwrite_mode": checkpoint.overwrite_mode,
                "need_notify": checkpoint.need_notify,
            }
        if phase == "decision" and kind == "plugin_transfer_intercept":
            stable_payload = {
                "source": source,
                "target_storage": checkpoint.target_storage,
                "target_path": checkpoint.final_target_path,
                "transfer_type": checkpoint.resolved_transfer_type,
            }
            return payload == stable_payload or (
                set(payload) == {*stable_payload, "over_flag"}
                and all(payload[key] == value for key, value in stable_payload.items())
                and isinstance(payload.get("over_flag"), bool)
            )
        if phase == "prepare" and kind == "discover_version_targets":
            return payload == {
                "storage": checkpoint.target_storage,
                "path": checkpoint.final_target_path,
            }
        if phase == "prepare" and kind == "delete_version_target":
            candidate = payload.get("item")
            if (
                    set(payload) != {"storage", "item"}
                    or payload.get("storage") != checkpoint.target_storage
                    or not isinstance(candidate, dict)
            ):
                return False
            return any(
                step.phase == "prepare"
                and step.kind == "discover_version_targets"
                and step.state == TransferStepState.SUCCEEDED.value
                and isinstance(step.result_payload, dict)
                and candidate in step.result_payload.get("items", [])
                for step in previous_steps
            )
        return False

    @classmethod
    def _validate_plan_steps(
            cls,
            *,
            task_id: str,
            checkpoint: TransferPlanCheckpoint,
            checkpoint_fingerprint: str,
            steps: list[TransferExecutionStepModel],
    ) -> None:
        """验证全部持久步骤属于冻结计划演进且身份与全局顺序未被篡改。"""
        if tuple(step.ordinal for step in steps) != tuple(range(len(steps))):
            raise TransferExecutionConflictError("整理步骤全局序号不连续")
        for index, step in enumerate(steps):
            if step.task_id != task_id:
                raise TransferExecutionConflictError("整理步骤绑定了错误任务")
            step_checkpoint = checkpoint
            if step.checkpoint_fingerprint != checkpoint_fingerprint:
                predecessor = cls._provider_predecessor_checkpoint(
                    checkpoint,
                    step,
                )
                if (
                        predecessor is None
                        or step.checkpoint_fingerprint != predecessor.fingerprint
                ):
                    raise TransferExecutionConflictError(
                        "整理步骤不属于当前冻结计划或合法 provider 前驱计划"
                    )
                step_checkpoint = predecessor
            if not cls._intent_belongs_to_checkpoint(
                    step_checkpoint,
                    phase=step.phase,
                    kind=step.kind,
                    payload=step.intent_payload,
                    previous_steps=steps[:index],
            ):
                raise TransferExecutionConflictError(
                    "整理步骤类型或参数不能由冻结计划导出"
                )
            expected_operation_id = build_transfer_operation_id(
                task_id=task_id,
                checkpoint_fingerprint=step.checkpoint_fingerprint,
                ordinal=step.ordinal,
                phase=step.phase,
                kind=step.kind,
                intent_payload=step.intent_payload,
            )
            if step.operation_id != expected_operation_id:
                raise TransferExecutionConflictError("整理步骤 operation ID 与冻结意图不一致")

    @classmethod
    def _validate_new_intent(
            cls,
            *,
            task_id: str,
            checkpoint: TransferPlanCheckpoint,
            checkpoint_fingerprint: str,
            steps: list[TransferExecutionStepModel],
            intent: TransferStepIntent,
    ) -> None:
        """验证待准备意图属于当前冻结计划，并且只追加或幂等重放既有序号。"""
        cls._validate_plan_steps(
            task_id=task_id,
            checkpoint=checkpoint,
            checkpoint_fingerprint=checkpoint_fingerprint,
            steps=steps,
        )
        if intent.checkpoint_fingerprint != checkpoint_fingerprint:
            raise TransferExecutionConflictError("整理步骤意图未绑定当前冻结计划指纹")
        if not cls._intent_belongs_to_checkpoint(
                checkpoint,
                phase=intent.phase,
                kind=intent.kind,
                payload=intent.payload,
                previous_steps=steps,
        ):
            raise TransferExecutionConflictError(
                "整理步骤意图类型或参数不能由冻结计划导出"
            )
        expected_operation_id = build_transfer_operation_id(
            task_id=task_id,
            checkpoint_fingerprint=intent.checkpoint_fingerprint,
            ordinal=intent.ordinal,
            phase=intent.phase,
            kind=intent.kind,
            intent_payload=intent.payload,
        )
        if intent.operation_id != expected_operation_id:
            raise TransferExecutionConflictError("整理步骤意图 operation ID 不可信")
        existing = next(
            (step for step in steps if step.operation_id == intent.operation_id),
            None,
        )
        if existing is not None:
            if not cls._intent_matches(existing, task_id=task_id, intent=intent):
                raise TransferExecutionConflictError(
                    "稳定 operation ID 已绑定不同步骤意图"
                )
            return
        if intent.ordinal != len(steps):
            raise TransferExecutionConflictError("整理步骤意图必须按全局序号连续追加")

    @staticmethod
    def _require_active_lease(
            pending: Optional[TransferPending],
            *,
            lease_token: str,
            now_utc: str,
    ) -> TransferPending:
        """返回仍由调用方持有的任务，并优先报告 lease fencing 失败。"""
        if (
                pending is None
                or pending.lease_token != lease_token
                or pending.lease_expires_at is None
                or pending.lease_expires_at <= now_utc
        ):
            raise TransferExecutionLeaseLostError("整理任务租约已失效或被接管")
        return pending

    @classmethod
    def _raise_fenced_failure(
            cls,
            pending: Optional[TransferPending],
            *,
            lease_token: str,
            now_utc: str,
            detail: str,
    ) -> None:
        """区分租约丢失与同租约内的状态或 attempt 冲突。"""
        cls._require_active_lease(
            pending,
            lease_token=lease_token,
            now_utc=now_utc,
        )
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
                pending = self._require_active_lease(
                    pending,
                    lease_token=lease_token,
                    now_utc=now_utc,
                )
                checkpoint, checkpoint_fingerprint = self._plan_identity(pending)
                oper = TransferExecutionStepOper(session)
                steps = self._execution_steps(
                    oper.list_by_task_id(task_id=task_id)
                )
                self._validate_new_intent(
                    task_id=task_id,
                    checkpoint=checkpoint,
                    checkpoint_fingerprint=checkpoint_fingerprint,
                    steps=steps,
                    intent=intent,
                )
                updated = pending_oper.stage_execution_running(
                    task_id=task_id,
                    lease_token=lease_token,
                    admission_state=pending.state,
                    checkpoint_version=checkpoint.schema_version,
                    checkpoint_payload=checkpoint.to_payload(),
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
                pending = self._require_active_lease(
                    pending,
                    lease_token=lease_token,
                    now_utc=now_utc,
                )
                checkpoint, checkpoint_fingerprint = self._plan_identity(pending)
                oper = TransferExecutionStepOper(session)
                steps = self._execution_steps(
                    oper.list_by_task_id(task_id=task_id)
                )
                self._validate_plan_steps(
                    task_id=task_id,
                    checkpoint=checkpoint,
                    checkpoint_fingerprint=checkpoint_fingerprint,
                    steps=steps,
                )
                if operation_id not in {step.operation_id for step in steps}:
                    raise TransferExecutionConflictError(
                        "待恢复步骤不属于冻结计划"
                    )
                pending_updated = pending_oper.stage_execution_running(
                    task_id=task_id,
                    lease_token=lease_token,
                    admission_state=pending.state,
                    checkpoint_version=checkpoint.schema_version,
                    checkpoint_payload=checkpoint.to_payload(),
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
                        message="整理任务已在等待重新处理",
                    )
                if state is not TransferExecutionState.FAILED:
                    message = (
                        "这条整理任务需要先完成人工确认，再重试"
                        if state is TransferExecutionState.MANUAL_REVIEW
                        else "这条整理任务当前无法重试，请刷新后再试"
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
                            message="整理任务已提交重试，请勿重复操作",
                        )
                    return TransferRetryRequestResult(
                        accepted=False,
                        state=state,
                        retry_generation=pending.retry_generation,
                        message="整理任务状态已变化，请刷新后重试",
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
                    message="已提交重新整理，后台将自动处理",
                )
                transaction.commit()
                return result
            except Exception:
                self._rollback(transaction)
                raise

    def discard_failed(
            self,
            *,
            task_id: str,
            history_id: int,
            settlement_revision: int,
    ) -> TransferFailureDiscardResult:
        """原子删除指定 FAILED pending、步骤证据并解除历史回执映射。"""
        with self._session_factory() as session:
            transaction = SqlAlchemyUnitOfWork(session)
            try:
                pending_oper = TransferPendingOper(session)
                history_oper = TransferHistoryOper(session)
                pending = pending_oper.get_by_task_id(task_id=task_id)
                if pending is None:
                    history = history_oper.get(history_id)
                    if history is None or history.transfer_task_id is None:
                        return TransferFailureDiscardResult(
                            discarded=True,
                            state=None,
                            message="整理任务已被其他操作放弃",
                        )
                    return TransferFailureDiscardResult(
                        discarded=False,
                        state=None,
                        message="没有找到对应的整理任务，请刷新后重试",
                    )

                state = TransferExecutionState(pending.execution_state)
                if state is not TransferExecutionState.FAILED:
                    message = (
                        "这条整理任务需要先完成人工确认，再重试"
                        if state is TransferExecutionState.MANUAL_REVIEW
                        else "这条整理任务当前无法放弃，请刷新后重试"
                    )
                    return TransferFailureDiscardResult(
                        discarded=False,
                        state=state,
                        message=message,
                    )
                if pending.lease_owner is not None or pending.lease_token is not None:
                    return TransferFailureDiscardResult(
                        discarded=False,
                        state=state,
                        message="整理任务正在处理中，暂时无法放弃，请稍后重试",
                    )
                if (
                        pending.terminal_history_id != history_id
                        or pending.settlement_revision != settlement_revision
                ):
                    return TransferFailureDiscardResult(
                        discarded=False,
                        state=state,
                        message="整理任务状态已变化，请刷新后重试",
                    )

                deleted = pending_oper.stage_delete_terminal_failure(
                    task_id=task_id,
                    history_id=history_id,
                    settlement_revision=settlement_revision,
                )
                if deleted != 1:
                    session.expire_all()
                    pending = pending_oper.get_by_task_id(task_id=task_id)
                    history = history_oper.get(history_id)
                    if pending is None and (
                            history is None or history.transfer_task_id is None
                    ):
                        return TransferFailureDiscardResult(
                            discarded=True,
                            state=None,
                            message="整理任务已被其他操作放弃",
                        )
                    return TransferFailureDiscardResult(
                        discarded=False,
                        state=(
                            TransferExecutionState(pending.execution_state)
                            if pending is not None
                            else None
                        ),
                        message="整理任务状态已变化，请刷新后重试",
                    )

                # PostgreSQL/启用外键的 SQLite 会级联删除；显式清理兼容独立测试库。
                TransferExecutionStepOper(session).stage_delete_task(task_id=task_id)
                detached = history_oper.stage_detach_failed_transfer_task(
                    history_id=history_id,
                    task_id=task_id,
                    settlement_revision=settlement_revision,
                )
                if detached != 1:
                    raise TransferExecutionConflictError(
                        "失败整理任务删除后无法解除历史回执映射"
                    )
                transaction.commit()
                return TransferFailureDiscardResult(
                    discarded=True,
                    state=TransferExecutionState.FAILED,
                    message="已放弃这条失败的整理任务",
                )
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
                pending_oper = TransferPendingOper(session)
                pending = pending_oper.get_by_task_id(task_id=task_id)
                pending = self._require_active_lease(
                    pending,
                    lease_token=lease_token,
                    now_utc=now_utc,
                )
                plan_checkpoint, plan_fingerprint = self._plan_identity(pending)
                step_oper = TransferExecutionStepOper(session)
                steps = self._execution_steps(
                    step_oper.list_by_task_id(task_id=task_id)
                )
                self._validate_plan_steps(
                    task_id=task_id,
                    checkpoint=plan_checkpoint,
                    checkpoint_fingerprint=plan_fingerprint,
                    steps=steps,
                )
                step_ids = tuple(step.operation_id for step in steps)
                if step_ids != checkpoint.operation_ids:
                    raise TransferExecutionConflictError(
                        "执行检查点引用的步骤顺序与持久步骤不一致"
                    )
                if any(step.state != TransferStepState.SUCCEEDED.value for step in steps):
                    raise TransferExecutionConflictError(
                        "存在未成功步骤，不能提交执行检查点"
                    )
                running = pending_oper.stage_execution_running(
                    task_id=task_id,
                    lease_token=lease_token,
                    admission_state=pending.state,
                    checkpoint_version=plan_checkpoint.schema_version,
                    checkpoint_payload=plan_checkpoint.to_payload(),
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
