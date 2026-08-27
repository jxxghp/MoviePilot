"""验证 settling 终态在崩溃后只重放持久结算。"""

import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.application.chain.events import TransferResultSettlement
from app.application.transfer.execution import (
    TransferExecutionCheckpoint,
    TransferExecutionCommand,
    TransferExecutionSnapshot,
    TransferExecutionState,
    TransferOperationObservation,
    TransferOperationObservationState,
    TransferStepIntent,
    TransferStepResult,
)
from app.application.transfer.workflow import (
    TransferAdmission,
    TransferPlanCheckpoint,
    TransferPlanItem,
    TransferPlanningInput,
    TransferTask,
)
from app.chain import transfer as transfer_chain_module
from app.chain.transfer import TransferChain
from app.db.adapters.chain import TransactionalChainDurableEventWriter
from app.db.adapters.transfer.admission import TransactionalTransferAdmissionRepository
from app.db.adapters.transfer.execution import TransactionalTransferExecutionRepository
from app.db.models.transferexecutionstep import TransferExecutionStep
from app.db.models.transferhistory import TransferHistory
from app.db.models.transferpending import TransferPending
from app.db.models.transfersettlementreceipt import TransferSettlementReceipt
from app.schemas.file import FileItem
from app.schemas.transfer import TransferInfo


def _planning_input(path: str) -> TransferPlanningInput:
    """构造保留源文件身份的最小持久规划输入。"""
    return TransferPlanningInput(
        source_fileitem={
            "storage": "local",
            "path": path,
            "type": "file",
            "name": Path(path).name,
            "basename": Path(path).stem,
            "extension": Path(path).suffix.lstrip("."),
            "size": 1024,
        },
        meta=None,
        mediainfo=None,
        requested_transfer_type="move",
    )


def _plan_checkpoint(
        planning_input: TransferPlanningInput,
) -> TransferPlanCheckpoint:
    """构造外部步骤已经结束后可直接结算的冻结计划。"""
    return TransferPlanCheckpoint(
        planning_input=planning_input,
        target_storage="local",
        root_target_path="/library",
        final_target_path="/library/Movie.mkv",
        resolved_transfer_type="move",
        items=(),
        need_notify=False,
        skip_reason="测试已完成外部步骤",
    )


def _transfer_result(path: str, *, success: bool) -> TransferInfo:
    """构造可完整写入 execution checkpoint 的整理结果。"""
    fileitem = FileItem(
        storage="local",
        path=path,
        type="file",
        name=Path(path).name,
        basename=Path(path).stem,
        extension=Path(path).suffix.lstrip("."),
        size=1024,
    )
    return TransferInfo(
        success=success,
        fileitem=fileitem,
        target_item=(
            FileItem(
                storage="local",
                path="/library/Movie.mkv",
                type="file",
                name="Movie.mkv",
            )
            if success
            else None
        ),
        transfer_type="move",
        fail_list=[] if success else [path],
        message="整理完成" if success else "整理失败",
        need_notify=False,
    )


def _execution_checkpoint(
        path: str,
        *,
        success: bool,
        include_transferinfo: bool = True,
) -> TransferExecutionCheckpoint:
    """构造成功或确定失败的聚合执行检查点。"""
    payload = {
        "outcome": "succeeded" if success else "failed",
        "error": None if success else "整理失败",
    }
    if include_transferinfo:
        payload["transferinfo"] = _transfer_result(
            path,
            success=success,
        ).model_dump(mode="json")
    return TransferExecutionCheckpoint.create(
        payload=payload,
        operation_ids=("operation-1",),
    )


def _add_settling_pending(
        factory,
        *,
        path: str,
        lease_state: str,
) -> tuple[TransferPlanCheckpoint, TransferExecutionCheckpoint]:
    """写入带完整计划和执行检查点的 settling 任务。"""
    planning_input = _planning_input(path)
    plan_checkpoint = _plan_checkpoint(planning_input)
    execution_checkpoint = _execution_checkpoint(path, success=True)
    lease_values = {
        "lease_owner": None,
        "lease_token": None,
        "lease_expires_at": None,
        "heartbeat_at": None,
        "attempt_count": 0,
    }
    if lease_state == "expired":
        lease_values.update({
            "lease_owner": "old-owner",
            "lease_token": "old-token",
            "lease_expires_at": "2000-01-01 00:00:00.000000",
            "heartbeat_at": "1999-12-31 23:59:00.000000",
            "attempt_count": 1,
        })
    with factory() as session:
        session.add(TransferPending(
            task_id="settling-task",
            storage="local",
            src_path=path,
            created_at="2026-08-27 09:00:00",
            state="planned",
            updated_at="2026-08-27 09:00:00",
            input_version=planning_input.schema_version,
            planning_input=planning_input.to_payload(),
            input_fingerprint=planning_input.fingerprint,
            checkpoint_version=plan_checkpoint.schema_version,
            checkpoint_payload=plan_checkpoint.to_payload(),
            planned_at="2026-08-27 09:00:00",
            execution_state="settling",
            execution_version=execution_checkpoint.version,
            execution_payload=execution_checkpoint.to_payload(),
            execution_fingerprint=execution_checkpoint.fingerprint,
            retry_generation=0,
            retry_count=0,
            settlement_revision=0,
            **lease_values,
        ))
        session.commit()
    return plan_checkpoint, execution_checkpoint


@pytest.fixture
def admission_store(tmp_path):
    """创建独立 SQLite admission 仓储及其 Session 工厂。"""
    engine = create_engine(f"sqlite:///{tmp_path / 'settling-recovery.db'}")
    TransferHistory.__table__.create(engine)
    TransferPending.__table__.create(engine)
    TransferExecutionStep.__table__.create(engine)
    TransferSettlementReceipt.__table__.create(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        yield TransactionalTransferAdmissionRepository(factory), factory
    finally:
        engine.dispose()


def _snapshot(
        checkpoint: TransferExecutionCheckpoint,
) -> TransferExecutionSnapshot:
    """构造 settling 状态的脱离 Session 执行投影。"""
    return TransferExecutionSnapshot(
        task_id="settling-task",
        state=TransferExecutionState.SETTLING,
        checkpoint=checkpoint,
        retry_generation=0,
        retry_count=0,
        retry_due_at=None,
        settlement_revision=0,
        terminal_history_id=None,
        last_error=None,
        steps=(),
    )


def _build_chain(admissions) -> TransferChain:
    """构造只允许执行 settling 终态恢复的 TransferChain 骨架。"""
    chain = object.__new__(TransferChain)
    chain._transfer_admissions = admissions
    chain._transfer_executions = MagicMock()
    chain._worker_owner_id = "recovery-owner"
    chain._owned_leases = {}
    chain._queued_lease_tokens = set()
    chain._worker_state_lock = threading.RLock()
    chain._closing = False
    chain._recovery_wakeup_event = threading.Event()
    chain._replay_stop_event = threading.Event()
    chain._lease_heartbeat_stop_event = threading.Event()
    chain._lease_heartbeat_thread = None
    chain._TransferChain__ensure_lease_heartbeat_owner = MagicMock()
    chain._TransferChain__ensure_recovery_scheduler = MagicMock()
    chain._TransferChain__restore_planned_task = MagicMock()
    chain._TransferChain__select_storage_oper = MagicMock(
        side_effect=AssertionError("settling 恢复不得选择存储适配器")
    )
    chain._plan_checkpoint_and_execute = MagicMock(
        side_effect=AssertionError("settling 恢复不得重新执行计划")
    )
    chain.jobview = MagicMock()
    return chain


def _recovered_task(
        chain: TransferChain,
        admission: TransferAdmission,
        execution_checkpoint: TransferExecutionCheckpoint,
) -> TransferTask:
    """把 claim 投影绑定为只待终态 writer 处理的恢复任务。"""
    assert admission.planning_input is not None
    assert admission.checkpoint is not None
    assert admission.lease_owner is not None
    assert admission.lease_token is not None
    task = TransferTask(
        fileitem=FileItem.model_validate(
            admission.planning_input.source_fileitem
        )
    )
    task.bind_admission_task_id(admission.task_id)
    task.bind_planning_input(admission.planning_input)
    task.bind_plan_checkpoint(admission.checkpoint)
    task.bind_execution_checkpoint(execution_checkpoint)
    task.bind_execution_lease(
        owner_id=admission.lease_owner,
        lease_token=admission.lease_token,
    )
    chain._owned_leases[admission.task_id] = (
        admission.lease_token,
        time.monotonic() + 120,
    )
    chain._transfer_executions.get_snapshot.return_value = TransferExecutionSnapshot(
        task_id=admission.task_id,
        state=TransferExecutionState.SETTLING,
        checkpoint=execution_checkpoint,
        retry_generation=0,
        retry_count=0,
        retry_due_at=None,
        settlement_revision=0,
        terminal_history_id=None,
        last_error=None,
        steps=(),
    )
    return task


@pytest.mark.parametrize("lease_state", ["missing", "expired"])
def test_settling_task_can_be_claimed_by_only_one_owner(
        admission_store,
        lease_state,
) -> None:
    """空租约和过期租约的 settling 任务都只能由一个 worker 取得。"""
    repository, factory = admission_store
    _add_settling_pending(
        factory,
        path="/downloads/Movie.mkv",
        lease_state=lease_state,
    )

    claimed = repository.claim_recoverable(
        owner_id="first-owner",
        limit=1,
        lease_seconds=120,
    )
    competing = repository.claim_recoverable(
        owner_id="second-owner",
        limit=1,
        lease_seconds=120,
    )

    assert len(claimed) == 1
    assert claimed[0].lease_owner == "first-owner"
    assert competing == []
    with factory() as session:
        pending = session.execute(select(TransferPending)).scalar_one()
        assert pending.execution_state == "settling"
        assert pending.lease_token == claimed[0].lease_token


@pytest.mark.parametrize(
    ("success", "include_transferinfo"),
    [(True, True), (False, False)],
)
def test_settling_result_calls_only_task_aware_terminal_writer(
        success,
        include_transferinfo,
) -> None:
    """成功和确定失败都只从检查点恢复结果并调用携带 task 的 writer。"""
    path = "/downloads/Movie.mkv"
    planning_input = _planning_input(path)
    plan_checkpoint = _plan_checkpoint(planning_input)
    execution_checkpoint = _execution_checkpoint(
        path,
        success=success,
        include_transferinfo=include_transferinfo,
    )
    admission = TransferAdmission(
        task_id="settling-task",
        storage="local",
        src_path=path,
        state="planned",
        created_at="2026-08-27 09:00:00",
        updated_at="2026-08-27 09:00:00",
        planning_input=planning_input,
        checkpoint=plan_checkpoint,
        lease_owner="recovery-owner",
        lease_token="recovery-token",
    )
    chain = _build_chain(MagicMock())
    task = _recovered_task(chain, admission, execution_checkpoint)
    writer_calls = []

    def terminal_writer(
            callback_task: TransferTask,
            transferinfo: TransferInfo,
    ) -> tuple[bool, str]:
        """记录 task-aware 终态 writer 收到的恢复事实。"""
        writer_calls.append((callback_task, transferinfo))
        return transferinfo.success, transferinfo.message or ""

    result = chain._TransferChain__handle_planned_transfer(
        task,
        terminal_writer,
    )

    assert result[0] is success
    assert len(writer_calls) == 1
    assert writer_calls[0][0] is task
    assert writer_calls[0][0].execution_checkpoint == execution_checkpoint
    assert writer_calls[0][1].success is success
    chain._TransferChain__select_storage_oper.assert_not_called()
    chain._plan_checkpoint_and_execute.assert_not_called()


def test_replay_settling_uses_frozen_source_without_filesystem_probe(
        monkeypatch,
) -> None:
    """move 后源文件已消失时，settling 回放仍应直接入队结算。"""
    path = "/already-moved/Movie.mkv"
    planning_input = _planning_input(path)
    plan_checkpoint = _plan_checkpoint(planning_input)
    execution_checkpoint = _execution_checkpoint(path, success=True)
    admission = TransferAdmission(
        task_id="settling-task",
        storage="local",
        src_path=path,
        state="planned",
        created_at="2026-08-27 09:00:00",
        updated_at="2026-08-27 09:00:00",
        planning_input=planning_input,
        checkpoint=plan_checkpoint,
        lease_owner="recovery-owner",
        lease_token="recovery-token",
    )
    admissions = MagicMock()
    admissions.claim_recoverable.return_value = [admission]
    executions = MagicMock()
    executions.get_snapshot.return_value = _snapshot(execution_checkpoint)
    chain = _build_chain(admissions)
    chain._transfer_executions = executions
    chain.put_to_queue = MagicMock(return_value=True)

    def reject_stat(*_args, **_kwargs):
        """任何源文件探测都表示 settling 恢复走回了旧执行路径。"""
        pytest.fail("settling 恢复不得探测已经移动的源文件")

    monkeypatch.setattr(Path, "stat", reject_stat)

    chain._TransferChain__replay_pending()

    queued_task = chain.put_to_queue.call_args.args[0]
    assert queued_task.fileitem.path == path
    assert queued_task.execution_checkpoint == execution_checkpoint
    admissions.abandon_unstarted.assert_not_called()
    admissions.release_claim.assert_not_called()
    chain._plan_checkpoint_and_execute.assert_not_called()


def test_writer_failure_releases_and_reclaims_same_settling_checkpoint(
        admission_store,
) -> None:
    """writer 临时失败后释放租约，再次恢复不得重新执行外部步骤。"""
    repository, factory = admission_store
    _, execution_checkpoint = _add_settling_pending(
        factory,
        path="/downloads/Movie.mkv",
        lease_state="missing",
    )
    first_admission = repository.claim_recoverable(
        owner_id="recovery-owner",
        limit=1,
        lease_seconds=120,
    )[0]
    chain = _build_chain(repository)
    first_task = _recovered_task(
        chain,
        first_admission,
        execution_checkpoint,
    )

    def unavailable_writer(*_args, **_kwargs):
        """模拟历史与 pending 原子 writer 的暂时性数据库失败。"""
        raise RuntimeError("writer temporarily unavailable")

    with pytest.raises(RuntimeError, match="temporarily unavailable"):
        chain._TransferChain__handle_planned_transfer(
            first_task,
            unavailable_writer,
        )
    assert chain._TransferChain__release_task_claim(
        first_task,
        error="writer temporarily unavailable",
    )

    chain._worker_owner_id = "second-recovery-owner"
    second_admission = repository.claim_recoverable(
        owner_id="second-recovery-owner",
        limit=1,
        lease_seconds=120,
    )[0]
    second_task = _recovered_task(
        chain,
        second_admission,
        execution_checkpoint,
    )
    settled = []

    def available_writer(
            callback_task: TransferTask,
            transferinfo: TransferInfo,
    ) -> tuple[bool, str]:
        """模拟下一轮恢复时恢复正常的 task-aware writer。"""
        settled.append((callback_task, transferinfo))
        return transferinfo.success, transferinfo.message or ""

    result = chain._TransferChain__handle_planned_transfer(
        second_task,
        available_writer,
    )

    assert result == (True, "整理完成")
    assert len(settled) == 1
    assert settled[0][0].execution_checkpoint == execution_checkpoint
    assert second_admission.lease_token != first_admission.lease_token
    chain._TransferChain__select_storage_oper.assert_not_called()
    chain._plan_checkpoint_and_execute.assert_not_called()


def test_bound_checkpoint_is_not_restored_outside_settling() -> None:
    """旧终态检查点留在 retry_wait 时不得跳过步骤恢复直接再次结算。"""
    path = "/downloads/retry-state.mkv"
    planning_input = _planning_input(path)
    plan_checkpoint = _plan_checkpoint(planning_input)
    execution_checkpoint = _execution_checkpoint(path, success=False)
    admission = TransferAdmission(
        task_id="retry-task",
        storage="local",
        src_path=path,
        state="planned",
        created_at="2026-08-27 09:00:00",
        updated_at="2026-08-27 09:00:00",
        planning_input=planning_input,
        checkpoint=plan_checkpoint,
        lease_owner="recovery-owner",
        lease_token="retry-token",
    )
    chain = _build_chain(MagicMock())
    task = _recovered_task(chain, admission, execution_checkpoint)
    chain._transfer_executions.get_snapshot.return_value = TransferExecutionSnapshot(
        task_id=admission.task_id,
        state=TransferExecutionState.RETRY_WAIT,
        checkpoint=execution_checkpoint,
        retry_generation=1,
        retry_count=1,
        retry_due_at="2026-08-27 09:00:00.000000",
        settlement_revision=1,
        terminal_history_id=41,
        last_error="copy failed",
        steps=(),
    )

    restored = chain._TransferChain__restore_settling_transfer_result(task)

    assert restored is None


def test_failed_settlement_retry_replays_step_and_commits_new_receipt(
        admission_store,
) -> None:
    """失败结算请求重试后应恢复 FAILED 步骤，并以新检查点完成下一版结算。"""
    admissions, factory = admission_store
    path = "/downloads/retry-success.mkv"
    planning_input = _planning_input(path)
    plan_checkpoint = TransferPlanCheckpoint(
        planning_input=planning_input,
        target_storage="local",
        root_target_path="/library",
        final_target_path="/library/retry-success.mkv",
        resolved_transfer_type="copy",
        items=(TransferPlanItem(
            sequence=0,
            source_fileitem=planning_input.source_fileitem,
            target_storage="local",
            target_path="/library/retry-success.mkv",
        ),),
        need_notify=False,
    )
    admitted = admissions.admit(
        storage="local",
        src_path=path,
        planning_input=planning_input,
    )
    first_claim = admissions.claim_task(
        task_id=admitted.task_id,
        owner_id="first-owner",
        lease_seconds=120,
    )
    assert first_claim is not None
    assert first_claim.lease_token is not None
    admissions.checkpoint_plan(
        task_id=admitted.task_id,
        lease_token=first_claim.lease_token,
        input_fingerprint=planning_input.fingerprint,
        checkpoint=plan_checkpoint,
    )
    executions = TransactionalTransferExecutionRepository(factory)
    command = TransferExecutionCommand(
        executions,
        attempt_token_factory=iter(("attempt-1", "attempt-2")).__next__,
    )
    plan_fingerprint = (
        TransferChain._TransferChain__transfer_plan_fingerprint(plan_checkpoint)
    )
    intent = TransferStepIntent.create(
        task_id=admitted.task_id,
        checkpoint_fingerprint=plan_fingerprint,
        ordinal=0,
        phase="transfer",
        kind="copy",
        payload={"source": path, "target": "/library/retry-success.mkv"},
    )
    prepared = command.prepare(
        task_id=admitted.task_id,
        lease_token=first_claim.lease_token,
        intent=intent,
    )
    started = command.begin(
        task_id=admitted.task_id,
        lease_token=first_claim.lease_token,
        operation_id=prepared.operation_id,
    )
    exhausted = command.exhaust(
        task_id=admitted.task_id,
        lease_token=first_claim.lease_token,
        step=started,
        error="copy failed",
        evidence=TransferStepResult(payload={"target_exists": False}),
    )
    assert exhausted.checkpoint is not None
    writer = TransactionalChainDurableEventWriter(factory)
    first_settlement = writer.transfer_result(
        topic=None,
        stage_history=lambda repository: repository.add_force(
            src=path,
            src_storage="local",
            status=False,
            errmsg="copy failed",
        ),
        event_payload={},
        publish=None,
        settlement=TransferResultSettlement(
            task_id=admitted.task_id,
            lease_token=first_claim.lease_token,
            execution_fingerprint=exhausted.checkpoint.fingerprint,
            outcome="failed",
            error="copy failed",
        ),
    )
    assert first_settlement is not None
    retry = command.request_retry(
        task_id=admitted.task_id,
        reason="manual retry",
        requested_by="test",
    )
    assert retry.accepted is True

    chain = _build_chain(admissions)
    chain._transfer_executions = executions
    chain.put_to_queue = MagicMock(return_value=True)
    chain._TransferChain__replay_pending()

    replayed_task = chain.put_to_queue.call_args.args[0]
    assert replayed_task.execution_checkpoint is None
    assert replayed_task.lease_token is not None
    runner = transfer_chain_module._DurableTransferStepRunner(
        task_id=admitted.task_id,
        lease_token=replayed_task.lease_token,
        checkpoint_fingerprint=plan_fingerprint,
        repository=executions,
    )
    resumed = []
    step_result = runner.run(
        phase="transfer",
        kind="copy",
        payload={"source": path, "target": "/library/retry-success.mkv"},
        execute=lambda: resumed.append("executed") or TransferStepResult(
            payload={"target_exists": True}
        ),
        observe=lambda: TransferOperationObservation(
            state=TransferOperationObservationState.NOT_APPLIED,
            evidence=TransferStepResult(payload={"target_exists": False}),
        ),
    )
    assert step_result.payload == {"target_exists": True}
    assert resumed == ["executed"]
    new_checkpoint = runner.checkpoint(_transfer_result(path, success=True))
    assert new_checkpoint.fingerprint != exhausted.checkpoint.fingerprint
    second_settlement = writer.transfer_result(
        topic=None,
        stage_history=lambda repository: repository.add_force(
            src=path,
            src_storage="local",
            status=True,
            errmsg=None,
        ),
        event_payload={},
        publish=None,
        settlement=TransferResultSettlement(
            task_id=admitted.task_id,
            lease_token=replayed_task.lease_token,
            execution_fingerprint=new_checkpoint.fingerprint,
            outcome="succeeded",
        ),
    )
    assert second_settlement is not None

    with factory() as session:
        receipts = session.scalars(
            select(TransferSettlementReceipt).order_by(
                TransferSettlementReceipt.settlement_revision
            )
        ).all()
        pending = session.scalar(select(TransferPending))
        steps = session.scalars(select(TransferExecutionStep)).all()
    assert [receipt.outcome for receipt in receipts] == ["failed", "succeeded"]
    assert pending is None
    assert steps == []
