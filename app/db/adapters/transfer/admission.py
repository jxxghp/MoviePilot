"""整理任务持久准入端口的 SQLAlchemy 适配器。"""

import logging
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from json import JSONDecodeError
from typing import Optional
from uuid import uuid4

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.application.transfer.workflow import (
    TRANSFER_ADMISSION_ACCEPTED,
    TRANSFER_ADMISSION_PLANNED,
    TRANSFER_ADMISSION_PROVIDER_PENDING,
    TransferAdmission,
    TransferAdmissionConflictError,
    TransferAdmissionProjectionError,
    TransferLeaseLostError,
    TransferPlanCheckpoint,
    TransferPlanningInput,
    TransferPlanningStateError,
)
from app.db.models.transferpending import TransferPending
from app.db.oper.transferpending import TransferPendingOper
from app.db.uow import SqlAlchemyUnitOfWork

_diagnostic_logger = logging.getLogger(__name__)


class TransactionalTransferAdmissionRepository:
    """以短生命周期 Session 实现整理任务持久准入端口。"""

    _MAX_RECOVERY_SCAN_TASKS = 5000

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        """保存由组合根提供的同步会话工厂。"""
        self._session_factory = session_factory

    @staticmethod
    def _now() -> str:
        """生成与历史登记时间可按字典序比较的当前时间。"""
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def _lease_now() -> datetime:
        """生成不受宿主时区影响的当前 UTC 租约时间。"""
        return datetime.now(timezone.utc)

    @staticmethod
    def _format_lease_time(value: datetime) -> str:
        """把 UTC 时间编码为可稳定排序的固定宽度字符串。"""
        return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")

    @staticmethod
    def _validate_claim_arguments(*, owner_id: str, lease_seconds: int) -> None:
        """拒绝无法建立有效租约身份或正向期限的调用。"""
        if not owner_id:
            raise ValueError("整理任务 claim 缺少 owner_id")
        if lease_seconds <= 0:
            raise ValueError("整理任务 lease_seconds 必须大于零")

    @staticmethod
    def _recoverable_states() -> tuple[str, ...]:
        """返回允许被 worker claim 的稳定业务状态。"""
        return (
            TRANSFER_ADMISSION_ACCEPTED,
            TRANSFER_ADMISSION_PROVIDER_PENDING,
            TRANSFER_ADMISSION_PLANNED,
        )

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
            lease_owner=pending.lease_owner,
            lease_token=pending.lease_token,
            lease_expires_at=pending.lease_expires_at,
            heartbeat_at=pending.heartbeat_at,
            attempt_count=pending.attempt_count,
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
            planning_input: TransferPlanningInput,
    ) -> TransferAdmission:
        """按输入指纹幂等持久化准入事实，并返回跨重启稳定身份。"""
        if not storage or not src_path:
            raise ValueError("整理任务的存储与源路径不能为空")
        if (
                planning_input.source_fileitem.get("storage") != storage
                or planning_input.source_fileitem.get("path") != src_path
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
                        input_version=planning_input.schema_version,
                        planning_input=planning_input.to_payload(),
                        input_fingerprint=planning_input.fingerprint,
                    )
                    if pending is None:
                        raise TransferAdmissionConflictError(
                            f"整理源文件已有持久终态回执: {storage}:{src_path}"
                        )
                    session.flush()
                    self._assert_input_match(pending, planning_input)
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
                self._assert_input_match(pending, planning_input)
                return self._project(pending)

    def claim_task(
            self,
            *,
            task_id: str,
            owner_id: str,
            lease_seconds: int,
    ) -> Optional[TransferAdmission]:
        """原子 claim 指定任务，任何已存在的有效租约都拒绝重复领取。"""
        self._validate_claim_arguments(
            owner_id=owner_id,
            lease_seconds=lease_seconds,
        )
        if not task_id:
            raise ValueError("整理任务 claim 缺少 task_id")
        now = self._lease_now()
        now_time = self._format_lease_time(now)
        lease_expires_at = self._format_lease_time(
            now + timedelta(seconds=lease_seconds)
        )
        with self._session_factory() as session:
            transaction = SqlAlchemyUnitOfWork(session)
            try:
                oper = TransferPendingOper(db=session)
                lease_token = uuid4().hex
                updated = oper.stage_claim_task(
                    task_id=task_id,
                    states=self._recoverable_states(),
                    owner_id=owner_id,
                    lease_token=lease_token,
                    now_time=now_time,
                    lease_expires_at=lease_expires_at,
                    updated_at=self._now(),
                )
                session.flush()
                session.expire_all()
                try:
                    pending = oper.get_by_task_id(task_id=task_id)
                except JSONDecodeError as error:
                    raise TransferAdmissionProjectionError(
                        f"整理任务持久 JSON 无法解码: {task_id} - {error}"
                    ) from error
                if updated:
                    if pending is None:
                        raise TransferPlanningStateError(
                            f"claim 后未找到整理任务: {task_id}"
                        )
                    try:
                        admission = self._project(pending)
                    except (
                            TransferAdmissionConflictError,
                            TransferPlanningStateError,
                            TypeError,
                            ValueError,
                    ) as error:
                        raise TransferAdmissionProjectionError(
                            f"整理任务持久投影损坏: {task_id} - {error}"
                        ) from error
                    transaction.commit()
                    return admission
                transaction.commit()
                return None
            except Exception:
                transaction.rollback()
                raise

    def claim_recoverable(
            self,
            *,
            owner_id: str,
            limit: int,
            lease_seconds: int,
    ) -> list[TransferAdmission]:
        """按登记顺序逐条 CAS claim 未租用或租约已过期的恢复任务。"""
        self._validate_claim_arguments(
            owner_id=owner_id,
            lease_seconds=lease_seconds,
        )
        if limit <= 0:
            return []
        claimed: list[TransferAdmission] = []
        after_cursor: Optional[tuple[str, int]] = None
        scanned_count = 0
        scan_limit = self._MAX_RECOVERY_SCAN_TASKS
        while len(claimed) < limit and scanned_count < scan_limit:
            candidate_limit = min(
                limit - len(claimed),
                scan_limit - scanned_count,
            )
            now_time = self._format_lease_time(self._lease_now())
            with self._session_factory() as session:
                candidates = TransferPendingOper(db=session).list_claimable_candidates(
                    states=self._recoverable_states(),
                    now_time=now_time,
                    limit=candidate_limit,
                    after_cursor=after_cursor,
                )
            if not candidates:
                break
            scanned_count += len(candidates)
            _, cursor_created_at, cursor_id = candidates[-1]
            after_cursor = (cursor_created_at, cursor_id)
            for task_id, _, _ in candidates:
                try:
                    admission = self.claim_task(
                        task_id=task_id,
                        owner_id=owner_id,
                        lease_seconds=lease_seconds,
                    )
                except TransferAdmissionProjectionError as error:
                    self._record_projection_failure(
                        task_id=task_id,
                        error=error,
                    )
                    continue
                if admission is not None:
                    claimed.append(admission)
                if len(claimed) >= limit:
                    break
        return claimed

    def _record_projection_failure(
            self,
            *,
            task_id: str,
            error: TransferAdmissionProjectionError,
    ) -> bool:
        """以独立 CAS 留存变化后的投影错误，并仅为新诊断记一次运行日志。"""
        diagnostic = f"恢复投影失败: {error}"
        with self._session_factory() as session:
            transaction = SqlAlchemyUnitOfWork(session)
            try:
                recorded = TransferPendingOper(
                    db=session
                ).stage_record_projection_failure(
                    task_id=task_id,
                    states=self._recoverable_states(),
                    error=diagnostic,
                    now_time=self._format_lease_time(self._lease_now()),
                    updated_at=self._now(),
                )
                transaction.commit()
            except Exception:
                transaction.rollback()
                raise
        if recorded:
            _diagnostic_logger.error(
                f"整理恢复任务投影损坏：task_id={task_id}, error={error}"
            )
        return bool(recorded)

    def heartbeat(
            self,
            *,
            task_id: str,
            lease_token: str,
            lease_seconds: int,
    ) -> Optional[TransferAdmission]:
        """仅以当前且未过期的 token 续租，禁止陈旧 worker 复活租约。"""
        if not task_id or not lease_token:
            raise ValueError("整理任务 heartbeat 缺少任务或租约身份")
        if lease_seconds <= 0:
            raise ValueError("整理任务 lease_seconds 必须大于零")
        now = self._lease_now()
        now_time = self._format_lease_time(now)
        lease_expires_at = self._format_lease_time(
            now + timedelta(seconds=lease_seconds)
        )
        with self._session_factory() as session:
            transaction = SqlAlchemyUnitOfWork(session)
            try:
                oper = TransferPendingOper(db=session)
                updated = oper.stage_heartbeat(
                    task_id=task_id,
                    lease_token=lease_token,
                    now_time=now_time,
                    lease_expires_at=lease_expires_at,
                )
                if not updated:
                    transaction.commit()
                    return None
                session.flush()
                session.expire_all()
                pending = oper.get_by_task_id(task_id=task_id)
                if pending is None:
                    raise TransferPlanningStateError(
                        f"heartbeat 后未找到整理任务: {task_id}"
                    )
                admission = self._project(pending)
                transaction.commit()
                return admission
            except Exception:
                transaction.rollback()
                raise

    def release_claim(
            self,
            *,
            task_id: str,
            lease_token: str,
            error: Optional[str] = None,
    ) -> bool:
        """仅以当前未过期 token 释放租约，陈旧 worker 不得改变任务。"""
        if not task_id or not lease_token:
            return False
        with self._session_factory() as session:
            transaction = SqlAlchemyUnitOfWork(session)
            try:
                released = TransferPendingOper(db=session).stage_release_claim(
                    task_id=task_id,
                    lease_token=lease_token,
                    error=error,
                    now_time=self._format_lease_time(self._lease_now()),
                    updated_at=self._now(),
                )
                transaction.commit()
                return bool(released)
            except Exception:
                transaction.rollback()
                raise

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
            lease_token: str,
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
                    lease_token=lease_token,
                    now_time=self._format_lease_time(self._lease_now()),
                    updated_at=self._now(),
                )
                session.flush()
                session.expire_all()
                pending = oper.get_by_task_id(task_id=task_id)
                if pending is None:
                    raise TransferPlanningStateError(f"未找到整理任务: {task_id}")
                if pending.input_fingerprint != input_fingerprint:
                    raise TransferAdmissionConflictError("整理任务输入指纹已经改变")
                now_time = self._format_lease_time(self._lease_now())
                if (
                        pending.lease_token != lease_token
                        or not pending.lease_expires_at
                        or pending.lease_expires_at <= now_time
                ):
                    raise TransferLeaseLostError("整理任务租约已过期或已被其他 worker 接管")
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

    def record_planning_failure(
            self,
            *,
            task_id: str,
            lease_token: str,
            error: str,
    ) -> None:
        """独立提交规划错误并保持任务处于接纳态供恢复重试。"""
        with self._session_factory() as session:
            transaction = SqlAlchemyUnitOfWork(session)
            try:
                updated = TransferPendingOper(db=session).stage_record_planning_failure(
                    task_id=task_id,
                    lease_token=lease_token,
                    error=error,
                    now_time=self._format_lease_time(self._lease_now()),
                    updated_at=self._now(),
                )
                if not updated:
                    raise TransferLeaseLostError(
                        "整理任务租约已过期或已被其他 worker 接管"
                    )
                transaction.commit()
            except Exception:
                transaction.rollback()
                raise

    def abandon_unstarted(self, *, task_id: str, lease_token: str) -> int:
        """仅以当前 token 删除无执行证据的缺失源任务，拒绝陈旧 worker。"""
        if not task_id or not lease_token:
            return 0
        with self._session_factory() as session:
            transaction = SqlAlchemyUnitOfWork(session)
            try:
                deleted = TransferPendingOper(db=session).stage_abandon_unstarted(
                    task_id=task_id,
                    lease_token=lease_token,
                    now_time=self._format_lease_time(self._lease_now()),
                )
                transaction.commit()
                return deleted
            except Exception:
                transaction.rollback()
                raise
