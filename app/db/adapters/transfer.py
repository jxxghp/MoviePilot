"""整理任务持久准入端口的 SQLAlchemy 适配器。"""

from collections.abc import Callable
from datetime import datetime
from typing import Optional
from uuid import uuid4

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.application.transfer import (
    TRANSFER_ADMISSION_ACCEPTED,
    TRANSFER_ADMISSION_PLANNED,
    TRANSFER_ADMISSION_PROVIDER_PENDING,
    TransferAdmission,
    TransferAdmissionConflictError,
    TransferPlanCheckpoint,
    TransferPlanningInput,
    TransferPlanningStateError,
)
from app.db.models.transferpending import TransferPending
from app.db.oper.transferpending import TransferPendingOper
from app.db.uow import SqlAlchemyUnitOfWork


class TransactionalTransferAdmissionRepository:
    """以短生命周期 Session 实现整理任务持久准入端口。"""

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        """保存由组合根提供的同步会话工厂。"""
        self._session_factory = session_factory

    @staticmethod
    def _now() -> str:
        """生成与历史登记时间可按字典序比较的当前时间。"""
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def _project(pending: TransferPending) -> TransferAdmission:
        """在 Session 有效期内把 ORM 行冻结为应用层 DTO。"""
        created_at = pending.created_at or pending.updated_at
        planning_input = TransferPlanningInput.from_payload(pending.planning_input)
        if pending.input_version != planning_input.schema_version:
            raise TransferPlanningStateError("整理规划输入列版本与 JSON 版本不一致")
        if pending.input_fingerprint != planning_input.fingerprint:
            raise TransferAdmissionConflictError("整理规划输入 JSON 与持久指纹不一致")
        checkpoint = (
            TransferPlanCheckpoint.from_payload(pending.checkpoint_payload)
            if pending.checkpoint_payload is not None
            else None
        )
        if checkpoint is not None:
            if pending.checkpoint_version != checkpoint.schema_version:
                raise TransferPlanningStateError("整理检查点列版本与 JSON 版本不一致")
            if checkpoint.planning_input.fingerprint != pending.input_fingerprint:
                raise TransferAdmissionConflictError("整理检查点内嵌输入与准入指纹不一致")
        if pending.state in {
                TRANSFER_ADMISSION_PROVIDER_PENDING,
                TRANSFER_ADMISSION_PLANNED,
        } and checkpoint is None:
            raise TransferPlanningStateError("待执行任务缺少完整检查点")
        if pending.state == TRANSFER_ADMISSION_ACCEPTED and checkpoint is not None:
            raise TransferPlanningStateError("接纳态任务不能携带计划检查点")
        if (
                pending.state == TRANSFER_ADMISSION_PROVIDER_PENDING
                and checkpoint is not None
                and not checkpoint.is_provider_pending
        ):
            raise TransferPlanningStateError("provider_pending 状态缺少 provider 调用快照")
        if (
                pending.state == TRANSFER_ADMISSION_PLANNED
                and checkpoint is not None
                and checkpoint.is_provider_pending
        ):
            raise TransferPlanningStateError("planned 状态不能携带 provider-only 检查点")
        return TransferAdmission(
            task_id=pending.task_id,
            storage=pending.storage,
            src_path=pending.src_path,
            state=pending.state,
            created_at=created_at,
            updated_at=pending.updated_at,
            last_error=pending.last_error,
            input_fingerprint=pending.input_fingerprint,
            planning_input=planning_input,
            checkpoint=checkpoint,
        )

    @staticmethod
    def _assert_input_match(
            pending: TransferPending,
            planning_input: TransferPlanningInput,
    ) -> None:
        """拒绝同一源文件以不同规划输入复用既有任务身份。"""
        if pending.input_fingerprint != planning_input.fingerprint:
            raise TransferAdmissionConflictError(
                f"整理源文件已按不同输入准入: {pending.storage}:{pending.src_path}"
            )

    def admit(
            self,
            *,
            storage: str,
            src_path: str,
            planning_input: Optional[TransferPlanningInput] = None,
    ) -> TransferAdmission:
        """按输入指纹幂等持久化准入事实，并返回跨重启稳定身份。"""
        effective_input = planning_input or TransferPlanningInput.legacy(
            storage=storage,
            src_path=src_path,
        )
        if (
                effective_input.source_fileitem.get("storage") != storage
                or effective_input.source_fileitem.get("path") != src_path
        ):
            raise ValueError("整理规划输入的源文件身份与准入参数不一致")
        now_time = self._now()
        try:
            with self._session_factory() as session:
                transaction = SqlAlchemyUnitOfWork(session)
                try:
                    pending = TransferPendingOper(db=session).stage_admit(
                        task_id=uuid4().hex,
                        storage=storage,
                        src_path=src_path,
                        state=TRANSFER_ADMISSION_ACCEPTED,
                        now_time=now_time,
                        input_version=effective_input.schema_version,
                        planning_input=effective_input.to_payload(),
                        input_fingerprint=effective_input.fingerprint,
                    )
                    if pending is None:
                        raise ValueError("整理任务的存储与源路径不能为空")
                    session.flush()
                    self._assert_input_match(pending, effective_input)
                    admission = self._project(pending)
                    transaction.commit()
                    return admission
                except Exception:
                    transaction.rollback()
                    raise
        except IntegrityError as error:
            # 并发准入可能同时通过查询；唯一约束决定赢家，输家回读稳定身份。
            with self._session_factory() as session:
                pending = TransferPendingOper(db=session).get_by_identity(
                    storage=storage,
                    src_path=src_path,
                )
                if pending is None:
                    raise RuntimeError("并发准入冲突后未找到已提交记录") from error
                self._assert_input_match(pending, effective_input)
                return self._project(pending)

    def list_accepted(self, limit: int = 5000) -> list[TransferAdmission]:
        """在独立只读会话中投影等待恢复或执行的准入记录。"""
        with self._session_factory() as session:
            pending_items = TransferPendingOper(db=session).list_by_state(
                state=TRANSFER_ADMISSION_ACCEPTED,
                limit=limit,
            )
            return [self._project(pending) for pending in pending_items]

    def list_recoverable(self, limit: int = 5000) -> list[TransferAdmission]:
        """投影接纳、provider 待执行或已规划的全部可恢复任务。"""
        with self._session_factory() as session:
            pending_items = TransferPendingOper(db=session).list_by_states(
                states=(
                    TRANSFER_ADMISSION_ACCEPTED,
                    TRANSFER_ADMISSION_PROVIDER_PENDING,
                    TRANSFER_ADMISSION_PLANNED,
                ),
                limit=limit,
            )
            return [self._project(pending) for pending in pending_items]

    def record_enqueue_failure(self, *, task_id: str, error: str) -> None:
        """独立提交最近一次入队失败，保留准入记录供后续恢复。"""
        with self._session_factory() as session:
            transaction = SqlAlchemyUnitOfWork(session)
            try:
                TransferPendingOper(db=session).stage_record_enqueue_failure(
                    task_id=task_id,
                    error=error,
                    now_time=self._now(),
                )
                transaction.commit()
            except Exception:
                transaction.rollback()
                raise

    def checkpoint_plan(
            self,
            *,
            task_id: str,
            input_fingerprint: str,
            checkpoint: TransferPlanCheckpoint,
    ) -> TransferAdmission:
        """以输入指纹 CAS 保存 provider 调用快照或升级宿主计划。"""
        if checkpoint.planning_input.fingerprint != input_fingerprint:
            raise TransferAdmissionConflictError("检查点输入与准入输入指纹不一致")
        checkpoint_payload = checkpoint.to_payload()
        target_state = (
            TRANSFER_ADMISSION_PROVIDER_PENDING
            if checkpoint.is_provider_pending
            else TRANSFER_ADMISSION_PLANNED
        )
        source_states = (
            (TRANSFER_ADMISSION_ACCEPTED,)
            if checkpoint.is_provider_pending
            else (
                TRANSFER_ADMISSION_ACCEPTED,
                TRANSFER_ADMISSION_PROVIDER_PENDING,
            )
        )
        with self._session_factory() as session:
            transaction = SqlAlchemyUnitOfWork(session)
            try:
                oper = TransferPendingOper(db=session)
                updated = oper.stage_checkpoint_plan(
                    task_id=task_id,
                    input_fingerprint=input_fingerprint,
                    checkpoint_version=checkpoint.schema_version,
                    checkpoint_payload=checkpoint_payload,
                    source_states=source_states,
                    target_state=target_state,
                    now_time=self._now(),
                )
                session.flush()
                session.expire_all()
                pending = oper.get_by_task_id(task_id=task_id)
                if pending is None:
                    raise TransferPlanningStateError(f"未找到整理任务: {task_id}")
                if pending.input_fingerprint != input_fingerprint:
                    raise TransferAdmissionConflictError("整理任务输入指纹已经改变")
                if not updated and not (
                        pending.state == target_state
                        and pending.checkpoint_payload == checkpoint_payload
                ):
                    raise TransferPlanningStateError(
                        f"整理任务不能从状态 {pending.state} 保存 {target_state} 检查点"
                    )
                admission = self._project(pending)
                transaction.commit()
                return admission
            except Exception:
                transaction.rollback()
                raise

    def record_planning_failure(self, *, task_id: str, error: str) -> None:
        """独立提交规划错误并保持任务处于接纳态供恢复重试。"""
        with self._session_factory() as session:
            transaction = SqlAlchemyUnitOfWork(session)
            try:
                TransferPendingOper(db=session).stage_record_planning_failure(
                    task_id=task_id,
                    error=error,
                    now_time=self._now(),
                )
                transaction.commit()
            except Exception:
                transaction.rollback()
                raise

    def discard_task(self, *, task_id: str) -> int:
        """在独立事务中按稳定任务标识删除已到终态的准入记录。"""
        with self._session_factory() as session:
            transaction = SqlAlchemyUnitOfWork(session)
            try:
                deleted = TransferPendingOper(db=session).stage_discard_task(
                    task_id=task_id,
                )
                transaction.commit()
                return deleted
            except Exception:
                transaction.rollback()
                raise
