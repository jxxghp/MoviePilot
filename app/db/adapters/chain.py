"""Chain durable 事件写入端口的 SQLAlchemy 适配器。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.application.chain.durable_events import (
    ChainDurableEventWriter,
    TransferHistoryRef,
    TransferResultSettlement,
    download_added_event_key,
    snapshot_download_added,
    snapshot_transfer_result,
    transfer_result_event_key,
)
from app.application.history import TransferHistoryRecord, TransferHistoryWriter
from app.application.outbox import (
    DOWNLOAD_ADDED_TOPIC,
    DurableEventCommand,
    OutboxIntent,
)
from app.application.transfer_execution import (
    TransferExecutionConflictError,
    TransferExecutionLeaseLostError,
    TransferExecutionState,
    TransferSettlementResult,
)
from app.db.adapters.outbox import SqlAlchemyOutboxRepository
from app.db.models.transfersettlementreceipt import TransferSettlementReceipt
from app.db.oper.downloadhistory import DownloadHistoryOper
from app.db.oper.transferexecutionstep import TransferExecutionStepOper
from app.db.oper.transferhistory import TransferHistoryOper
from app.db.oper.transferpending import TransferPendingOper
from app.db.oper.transfersettlementreceipt import TransferSettlementReceiptOper
from app.db.uow import SqlAlchemyUnitOfWork


class _StagingTransferHistoryWriter:
    """让既有历史字段映射复用无提交的 replace 适配器。"""

    def __init__(
            self,
            repository: TransferHistoryOper,
            *,
            settlement: TransferResultSettlement | None = None,
            settlement_revision: int | None = None,
    ) -> None:
        """保存仓储，并让历史继续表达同源最新业务投影。"""
        self._repository = repository
        self._settlement = settlement
        self._settlement_revision = settlement_revision

    def get_by_src(
        self,
        src: str,
        storage: str | None = None,
    ) -> TransferHistoryRecord | None:
        """转发按源路径读取。"""
        return self._repository.get_by_src(src, storage)

    def get_success_by_src(
        self,
        src: str,
        storage: str | None = None,
    ) -> TransferHistoryRecord | None:
        """读取成功记录；任务结算时绑定当前任务投影。"""
        if self._settlement is not None:
            if self._settlement_revision is None:
                raise RuntimeError("整理任务结算缺少事务内修订号")
            return self._repository.stage_bind_settlement(
                task_id=self._settlement.task_id,
                settlement_revision=self._settlement_revision,
                src=src,
                storage=storage,
            )
        return self._repository.get_success_by_src(src, storage)

    def add_force(self, **payload: Any) -> TransferHistoryRecord:
        """保持旧端口名，并按是否存在任务身份选择暂存策略。"""
        if self._settlement is not None:
            if self._settlement_revision is None:
                raise RuntimeError("整理任务结算缺少事务内修订号")
            return self._repository.stage_upsert_by_transfer_task_id(
                task_id=self._settlement.task_id,
                settlement_revision=self._settlement_revision,
                retain_task_mapping=self._settlement.outcome == "failed",
                payload=payload,
            )
        return self._repository.stage_replace_by_src(**payload)


@dataclass(frozen=True, slots=True)
class _StagedTransferResult:
    """保存构造 outbox 所需历史投影及可选任务结算结果。"""

    history: TransferHistoryRef
    settlement: TransferSettlementResult | None = None


class TransactionalChainDurableEventWriter(ChainDurableEventWriter):
    """为每次 Chain 结果事件创建独占同步 Session 和 UoW。"""

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        """注入惰性同步 Session 工厂。"""
        self._session_factory = session_factory

    def download_added(
        self,
        *,
        history_payload: dict[str, Any],
        file_payloads: list[dict[str, Any]],
        event_payload: dict[str, Any],
        after_commit: Callable[[], None],
        publish: Callable[[dict[str, Any]], None],
    ) -> None:
        """原子写下载历史、文件清单和 DownloadAdded intent。"""
        session = self._session_factory()
        try:
            repository = DownloadHistoryOper(session)
            outbox = SqlAlchemyOutboxRepository(session)
            command = DurableEventCommand(
                unit_of_work=SqlAlchemyUnitOfWork(session),
                outbox=outbox,
            )
            def stage_business() -> int:
                """在同一事务暂存下载历史和可选文件清单。"""
                history = repository.stage_add(history_payload)
                if file_payloads:
                    repository.stage_add_files(file_payloads)
                return int(history.id)

            def build_intent(history_id: int) -> OutboxIntent:
                """历史 ID 确定后构造本次下载事实的稳定事件键。"""
                event_key = download_added_event_key(history_id)
                event_payload["idempotency_key"] = event_key
                return OutboxIntent(
                    event_key=event_key,
                    topic=DOWNLOAD_ADDED_TOPIC,
                    payload=snapshot_download_added(event_payload),
                )

            command.execute(
                intent=build_intent,
                stage_business=stage_business,
                after_commit=after_commit,
                publish=lambda: publish(event_payload),
            )
        finally:
            session.close()

    def transfer_result(
        self,
        *,
        topic: str | None,
        stage_history: Callable[[TransferHistoryWriter], TransferHistoryRecord | None],
        event_payload: dict[str, Any],
        publish: Callable[[dict[str, Any]], None] | None,
        settlement: TransferResultSettlement | None = None,
    ) -> TransferHistoryRecord | TransferSettlementResult | None:
        """原子写历史、可选任务终态与 intent，再返回稳定投影。"""
        if topic is None and settlement is None:
            raise ValueError("无事件 topic 的整理写入必须绑定 durable 任务结算")
        session = self._session_factory()
        try:
            history_repository = TransferHistoryOper(session)
            pending_repository = TransferPendingOper(session)
            receipt_repository = TransferSettlementReceiptOper(session)
            already_settled = self._read_settlement_result(
                pending_repository=pending_repository,
                receipt_repository=receipt_repository,
                settlement=settlement,
            )
            if already_settled is not None:
                return already_settled

            command = DurableEventCommand(
                unit_of_work=SqlAlchemyUnitOfWork(session),
                outbox=SqlAlchemyOutboxRepository(session),
            )

            def stage_business() -> _StagedTransferResult:
                """同一事务暂存历史及受 fencing 保护的 pending 终态。"""
                expected_revision = self._settlement_revision(
                    pending_repository=pending_repository,
                    settlement=settlement,
                )
                next_revision = (
                    expected_revision + 1
                    if expected_revision is not None
                    else None
                )
                staging = _StagingTransferHistoryWriter(
                    history_repository,
                    settlement=settlement,
                    settlement_revision=next_revision,
                )
                history = stage_history(staging)
                if history is None:
                    raise RuntimeError("整理历史暂存失败，无法登记 durable 结果事件")
                projected = TransferHistoryRef(
                    id=history.id,
                    status=bool(history.status),
                    src=history.src,
                    src_storage=history.src_storage,
                    src_fileitem=history.src_fileitem,
                )
                if settlement is None:
                    return _StagedTransferResult(history=projected)
                assert expected_revision is not None
                assert next_revision is not None
                self._validate_history_outcome(projected, settlement)
                pending_deleted = self._stage_pending_terminal(
                    session=session,
                    repository=pending_repository,
                    settlement=settlement,
                    expected_revision=expected_revision,
                    history_id=projected.id,
                )
                settled_at = datetime.now(timezone.utc).isoformat()
                receipt_repository.stage_append(
                    task_id=settlement.task_id,
                    history_id=projected.id,
                    settlement_revision=next_revision,
                    outcome=settlement.outcome,
                    execution_fingerprint=settlement.execution_fingerprint,
                    lease_token=settlement.lease_token,
                    history_status=projected.status,
                    src=projected.src,
                    src_storage=projected.src_storage,
                    pending_deleted=pending_deleted,
                    error=settlement.error,
                    settled_at=settled_at,
                )
                return _StagedTransferResult(
                    history=projected,
                    settlement=TransferSettlementResult(
                        history_id=projected.id,
                        settlement_revision=next_revision,
                        pending_deleted=pending_deleted,
                    ),
                )

            def build_intent(
                result: _StagedTransferResult,
            ) -> OutboxIntent:
                """历史 ID 确定后构造事件键与可恢复快照。"""
                assert topic is not None
                event_key = transfer_result_event_key(
                    topic,
                    result.history.id,
                    settlement=settlement,
                    settlement_revision=(
                        result.settlement.settlement_revision
                        if result.settlement is not None
                        else None
                    ),
                )
                event_payload["transfer_history_id"] = result.history.id
                event_payload["idempotency_key"] = event_key
                return OutboxIntent(
                    event_key=event_key,
                    topic=topic,
                    payload=snapshot_transfer_result(event_payload),
                )

            try:
                result = command.execute(
                    intent=build_intent if topic is not None else None,
                    stage_business=stage_business,
                    publish=(
                        (lambda: publish(event_payload))
                        if (
                            settlement is None
                            and topic is not None
                            and publish is not None
                        )
                        else None
                    ),
                )
            except (
                    IntegrityError,
                    TransferExecutionConflictError,
                    TransferExecutionLeaseLostError,
                    ValueError,
            ):
                if settlement is None:
                    raise
                session.rollback()
                replay = self._read_settlement_result(
                    pending_repository=pending_repository,
                    receipt_repository=receipt_repository,
                    settlement=settlement,
                )
                if replay is None:
                    raise
                return replay
            return result.settlement or result.history
        finally:
            session.close()

    @staticmethod
    def _read_settlement_result(
            *,
            pending_repository: TransferPendingOper,
            receipt_repository: TransferSettlementReceiptOper,
            settlement: TransferResultSettlement | None,
    ) -> TransferSettlementResult | None:
        """识别已提交终态并返回幂等结果，未结算时返回空。"""
        if settlement is None:
            return None
        pending = pending_repository.get_by_task_id(task_id=settlement.task_id)
        latest = receipt_repository.get_latest_by_task_id(
            task_id=settlement.task_id
        )
        receipt = receipt_repository.get_by_identity(
            task_id=settlement.task_id,
            execution_fingerprint=settlement.execution_fingerprint,
            lease_token=settlement.lease_token,
            outcome=settlement.outcome,
        )
        if (
                pending is not None
                and pending.execution_state == TransferExecutionState.FAILED.value
        ):
            if (
                    latest is None
                    or pending.terminal_history_id != latest.history_id
                    or pending.settlement_revision != latest.settlement_revision
                    or pending.execution_fingerprint != latest.execution_fingerprint
                    or latest.outcome != "failed"
                    or latest.pending_deleted
            ):
                raise TransferExecutionConflictError("失败终态与最新结算回执不一致")
        if receipt is not None:
            TransactionalChainDurableEventWriter._validate_receipt(
                receipt=receipt,
                settlement=settlement,
            )
            return TransferSettlementResult(
                history_id=receipt.history_id,
                settlement_revision=receipt.settlement_revision,
                pending_deleted=receipt.pending_deleted,
                already_settled=True,
            )
        if pending is None:
            if latest is None:
                raise TransferExecutionConflictError(
                    "pending 已不存在且没有可验证的终态回执"
                )
            raise TransferExecutionConflictError("整理终态与 durable 回执不一致")
        if pending.execution_state != TransferExecutionState.FAILED.value:
            return None
        raise TransferExecutionConflictError("失败终态缺少匹配的结算回执")

    @staticmethod
    def _validate_receipt(
            *,
            receipt: TransferSettlementReceipt,
            settlement: TransferResultSettlement,
    ) -> None:
        """校验重放请求与独立回执中的终态身份完全一致。"""
        expected_status = settlement.outcome == "succeeded"
        if (
                receipt.task_id != settlement.task_id
                or receipt.outcome != settlement.outcome
                or receipt.execution_fingerprint != settlement.execution_fingerprint
                or receipt.lease_token != settlement.lease_token
                or receipt.history_status is not expected_status
                or receipt.error != settlement.error
        ):
            raise TransferExecutionConflictError("整理终态与 durable 回执不一致")

    @staticmethod
    def _settlement_revision(
            *,
            pending_repository: TransferPendingOper,
            settlement: TransferResultSettlement | None,
    ) -> int | None:
        """从当前 pending 读取 CAS 基准修订号并校验执行身份。"""
        if settlement is None:
            return None
        pending = pending_repository.get_by_task_id(task_id=settlement.task_id)
        if pending is None:
            raise TransferExecutionLeaseLostError("整理任务 pending 已不存在")
        now_utc = TransactionalChainDurableEventWriter._format_utc(
            datetime.now(timezone.utc)
        )
        if (
                pending.lease_token != settlement.lease_token
                or pending.lease_expires_at is None
                or pending.lease_expires_at <= now_utc
        ):
            raise TransferExecutionLeaseLostError("整理任务租约已失效或被接管")
        if (
                pending.execution_state != TransferExecutionState.SETTLING.value
                or pending.execution_fingerprint
                != settlement.execution_fingerprint
        ):
            raise TransferExecutionConflictError("整理终态与执行检查点不匹配")
        return int(pending.settlement_revision)

    @staticmethod
    def _stage_pending_terminal(
            *,
            session: Session,
            repository: TransferPendingOper,
            settlement: TransferResultSettlement,
            expected_revision: int,
            history_id: int,
    ) -> bool:
        """以同一修订和 lease CAS 收口 pending，成功时同时清理步骤。"""
        now = datetime.now(timezone.utc)
        now_utc = TransactionalChainDurableEventWriter._format_utc(now)
        if settlement.outcome == "succeeded":
            TransferExecutionStepOper(session).stage_delete_task(
                task_id=settlement.task_id
            )
            updated = repository.stage_delete_terminal_success(
                task_id=settlement.task_id,
                lease_token=settlement.lease_token,
                execution_fingerprint=settlement.execution_fingerprint,
                expected_revision=expected_revision,
                now_utc=now_utc,
            )
            pending_deleted = True
        else:
            updated = repository.stage_terminal_failure(
                task_id=settlement.task_id,
                lease_token=settlement.lease_token,
                execution_fingerprint=settlement.execution_fingerprint,
                expected_revision=expected_revision,
                history_id=history_id,
                error=settlement.error,
                now_utc=now_utc,
                updated_at=now.astimezone().strftime("%Y-%m-%d %H:%M:%S"),
            )
            pending_deleted = False
        if updated != 1:
            session.expire_all()
            current = repository.get_by_task_id(task_id=settlement.task_id)
            if (
                    current is None
                    or current.lease_token != settlement.lease_token
                    or current.lease_expires_at is None
                    or current.lease_expires_at <= now_utc
            ):
                raise TransferExecutionLeaseLostError(
                    "整理任务租约已失效或被其他 worker 接管"
                )
            raise TransferExecutionConflictError("整理终态结算版本发生冲突")
        return pending_deleted

    @staticmethod
    def _validate_history_outcome(
            history: TransferHistoryRef,
            settlement: TransferResultSettlement,
    ) -> None:
        """拒绝结算终态与历史状态不一致的调用。"""
        expected_status = settlement.outcome == "succeeded"
        if history.status is not expected_status:
            raise TransferExecutionConflictError("整理终态与历史状态不一致")

    @staticmethod
    def _format_utc(value: datetime) -> str:
        """编码与 pending lease 列一致的固定宽度 UTC 时间。"""
        return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")
