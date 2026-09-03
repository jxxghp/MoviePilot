"""验证 TransferChain 步骤 runner 与文件执行器的崩溃恢复边界。"""

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.application.transfer.execution import (
    TransferExecutionCommand,
    TransferExecutionState,
    TransferOperationObservation,
    TransferOperationObservationState,
    TransferStepIntent,
    TransferStepResult,
    build_transfer_checkpoint_fingerprint,
)
from app.application.transfer.workflow import (
    TransferPlanCheckpoint,
    TransferPlanItem,
    TransferPlanningInput,
)
from app.chain.transfer import execution as transfer_chain_module
from app.db.adapters.transfer.execution import (
    TransactionalTransferExecutionRepository,
)
from app.db.base import Base
from app.db.models.transferexecutionstep import TransferExecutionStep
from app.db.models.transferhistory import TransferHistory
from app.db.models.transferpending import TransferPending
from app.modules.filemanager.transhandler import TransHandler
from app.schemas.workflow import FileItem


def _runner_plan_checkpoint() -> TransferPlanCheckpoint:
    """构造 runner fixture 使用的完整冻结计划。"""
    planning_input = TransferPlanningInput(
        source_fileitem={
            "storage": "local",
            "path": "/source.mkv",
            "type": "file",
        },
        target_storage="local",
        target_path="/target.mkv",
        requested_transfer_type="copy",
    )
    return TransferPlanCheckpoint(
        planning_input=planning_input,
        target_storage="local",
        root_target_path="/",
        final_target_path="/target.mkv",
        resolved_transfer_type="copy",
        items=(TransferPlanItem(
            sequence=0,
            source_fileitem=planning_input.source_fileitem,
            target_storage="local",
            target_path="/target.mkv",
        ),),
    )


@pytest.fixture
def execution_repository():
    """构造带有效 pending 租约的独立执行仓储。"""
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[
            TransferPending.__table__,
            TransferHistory.__table__,
            TransferExecutionStep.__table__,
        ],
    )
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    plan_checkpoint = _runner_plan_checkpoint()
    planning_input = plan_checkpoint.planning_input
    with factory() as session:
        session.add(TransferPending(
            task_id="task-runner",
            storage="local",
            src_path="/source.mkv",
            created_at="2026-08-27 09:00:00",
            state="planned",
            updated_at="2026-08-27 09:00:00",
            input_version=1,
            planning_input=planning_input.to_payload(),
            input_fingerprint=planning_input.fingerprint,
            checkpoint_version=plan_checkpoint.schema_version,
            checkpoint_payload=plan_checkpoint.to_payload(),
            planned_at="2026-08-27 09:00:00",
            lease_owner="worker",
            lease_token="lease",
            lease_expires_at="2099-01-01 00:00:00.000000",
            heartbeat_at="2026-08-27 01:00:00.000000",
            attempt_count=1,
            execution_state="not_started",
            retry_generation=0,
            retry_count=0,
            settlement_revision=0,
        ))
        session.commit()
    repository = TransactionalTransferExecutionRepository(
        factory,
        local_clock=lambda: datetime(2026, 8, 27, 9, 30, 0),
        lease_clock=lambda: datetime(2026, 8, 27, 1, 30, 0, tzinfo=timezone.utc),
    )
    try:
        yield repository
    finally:
        engine.dispose()


def _runner(repository):
    """构造绑定固定任务、租约与计划身份的 durable runner。"""
    return transfer_chain_module._DurableTransferStepRunner(
        task_id="task-runner",
        lease_token="lease",
        checkpoint_fingerprint=_runner_plan_fingerprint(),
        repository=repository,
    )


def _runner_plan_fingerprint() -> str:
    """返回 runner fixture 中完整冻结计划的 canonical 指纹。"""
    return build_transfer_checkpoint_fingerprint(
        _runner_plan_checkpoint().to_payload()
    )


def _runner_step_payload() -> dict:
    """返回由 runner 冻结计划唯一叶操作导出的目标落地参数。"""
    checkpoint = _runner_plan_checkpoint()
    item = checkpoint.items[0]
    return {
        "source": item.source_fileitem,
        "target_storage": item.target_storage,
        "target_path": item.target_path,
        "transfer_type": checkpoint.resolved_transfer_type,
    }


def test_runner_replay_returns_persisted_result_without_repeating_side_effect(
        execution_repository,
):
    """成功步骤重放只能回读结果，不能再次调用外部执行函数。"""
    calls = []
    first = _runner(execution_repository).run(
        phase="transfer",
        kind="materialize_target",
        payload=_runner_step_payload(),
        execute=lambda: calls.append("executed") or TransferStepResult(
            payload={"item": {"path": "/target.mkv"}}
        ),
        observe=lambda: pytest.fail("新步骤不应执行恢复探测"),
    )
    second = _runner(execution_repository).run(
        phase="transfer",
        kind="materialize_target",
        payload=_runner_step_payload(),
        execute=lambda: pytest.fail("已成功步骤不得重复执行"),
        observe=lambda: pytest.fail("已成功步骤不得执行恢复探测"),
    )
    assert first == second
    assert calls == ["executed"]


def test_runner_routes_unknown_orphaned_attempt_to_manual_review(
        execution_repository,
):
    """遗留 STARTED 无法严格判断时必须隔离，不能再次执行副作用。"""
    command = TransferExecutionCommand(execution_repository)
    prepared = command.prepare(
        task_id="task-runner",
        lease_token="lease",
        intent=TransferStepIntent.create(
            task_id="task-runner",
                checkpoint_fingerprint=_runner_plan_fingerprint(),
                ordinal=0,
                phase="transfer",
                kind="materialize_target",
                payload=_runner_step_payload(),
        ),
    )
    command.begin(
        task_id="task-runner",
        lease_token="lease",
        operation_id=prepared.operation_id,
    )

    with pytest.raises(
        transfer_chain_module._TransferManualReviewRequired,
        match="禁止自动重放",
    ):
        _runner(execution_repository).run(
            phase="transfer",
            kind="materialize_target",
            payload=_runner_step_payload(),
            execute=lambda: pytest.fail("未知遗留步骤不得重放"),
            observe=lambda: TransferOperationObservation(
                state=TransferOperationObservationState.UNKNOWN,
                evidence=TransferStepResult(payload={"receipt": None}),
            ),
        )
    snapshot = execution_repository.get_snapshot(task_id="task-runner")
    assert snapshot is not None
    assert snapshot.state is TransferExecutionState.MANUAL_REVIEW


def test_runner_observes_applied_after_execute_error_and_completes_step(
        execution_repository,
) -> None:
    """execute 抛错后若外部事实已生效，必须以观察证据完成而非重放。"""
    evidence = TransferStepResult(payload={"target": "/target.mkv"})

    def execute() -> TransferStepResult:
        """模拟副作用成功后调用方在返回前崩溃。"""
        raise OSError("connection reset after apply")

    result = _runner(execution_repository).run(
        phase="transfer",
        kind="materialize_target",
        payload=_runner_step_payload(),
        execute=execute,
        observe=lambda: TransferOperationObservation(
            state=TransferOperationObservationState.APPLIED,
            evidence=evidence,
        ),
    )

    snapshot = execution_repository.get_snapshot(task_id="task-runner")
    assert result == evidence
    assert snapshot is not None
    assert snapshot.state is TransferExecutionState.RUNNING
    assert snapshot.steps[0].result == evidence
    assert snapshot.steps[0].state.value == "succeeded"


def test_runner_defers_after_execute_error_observed_not_applied(
        execution_repository,
) -> None:
    """execute 抛错且确认未生效时只能进入持久退避，不得误判成功。"""
    evidence = TransferStepResult(payload={"target_exists": False})

    def execute() -> TransferStepResult:
        """模拟外部操作在应用前失败。"""
        raise OSError("write rejected")

    with pytest.raises(transfer_chain_module._TransferRetryDeferred):
        _runner(execution_repository).run(
            phase="transfer",
            kind="materialize_target",
            payload=_runner_step_payload(),
            execute=execute,
            observe=lambda: TransferOperationObservation(
                state=TransferOperationObservationState.NOT_APPLIED,
                evidence=evidence,
            ),
        )

    snapshot = execution_repository.get_snapshot(task_id="task-runner")
    assert snapshot is not None
    assert snapshot.state is TransferExecutionState.RETRY_WAIT
    assert snapshot.steps[0].result == evidence
    assert snapshot.steps[0].state.value == "failed"


@pytest.mark.parametrize(
    "observation_state",
    [
        TransferOperationObservationState.UNKNOWN,
        TransferOperationObservationState.CONFLICT,
    ],
)
def test_runner_freezes_uncertain_execute_error_for_manual_review(
        execution_repository,
        observation_state,
) -> None:
    """execute 异常后的未知或冲突结果必须冻结，禁止自动重放。"""
    evidence = TransferStepResult(payload={"receipt": "ambiguous"})

    def execute() -> TransferStepResult:
        """模拟外部结果未知的执行异常。"""
        raise TimeoutError("provider timeout")

    with pytest.raises(
        transfer_chain_module._TransferManualReviewRequired,
        match=observation_state.value,
    ):
        _runner(execution_repository).run(
            phase="transfer",
            kind="materialize_target",
            payload=_runner_step_payload(),
            execute=execute,
            observe=lambda: TransferOperationObservation(
                state=observation_state,
                evidence=evidence,
            ),
        )

    snapshot = execution_repository.get_snapshot(task_id="task-runner")
    assert snapshot is not None
    assert snapshot.state is TransferExecutionState.MANUAL_REVIEW
    assert snapshot.steps[0].result == evidence
    assert snapshot.steps[0].state.value == "manual_review"


def test_runner_freezes_when_observer_errors_after_execute_error(
        execution_repository,
) -> None:
    """execute 与 observer 同时失败时必须保留双重证据并进入人工复核。"""
    def execute() -> TransferStepResult:
        """模拟调用结果未知的执行超时。"""
        raise TimeoutError("execute timeout")

    def observe() -> TransferOperationObservation:
        """模拟外部状态查询端点同时不可用。"""
        raise ConnectionError("observer unavailable")

    with pytest.raises(
        transfer_chain_module._TransferManualReviewRequired,
        match="unknown",
    ):
        _runner(execution_repository).run(
            phase="transfer",
            kind="materialize_target",
            payload=_runner_step_payload(),
            execute=execute,
            observe=observe,
        )

    snapshot = execution_repository.get_snapshot(task_id="task-runner")
    assert snapshot is not None
    assert snapshot.state is TransferExecutionState.MANUAL_REVIEW
    assert snapshot.steps[0].result is not None
    assert snapshot.steps[0].result.payload == {
        "execute_error": "execute timeout",
        "observe_error": "observer unavailable",
    }


class _ImmediateStepRunner:
    """记录 TransHandler 拆分顺序并立即执行步骤的测试 runner。"""

    def __init__(self) -> None:
        """初始化步骤记录。"""
        self.steps = []

    def run(self, *, phase, kind, payload, execute, observe):
        """记录稳定意图并直接执行，不触发恢复探测。"""
        self.steps.append((phase, kind, payload))
        return execute()


def test_cross_storage_move_materializes_before_independent_source_delete(tmp_path):
    """跨存储 move 必须先复制目标，再用独立步骤删除源。"""
    source_path = tmp_path / "source.mkv"
    source_path.write_bytes(b"movie")
    source_item = FileItem(
        storage="local",
        path=source_path.as_posix(),
        name=source_path.name,
        type="file",
        size=source_path.stat().st_size,
        extension="mkv",
    )
    target_item = FileItem(
        storage="remote",
        path="/library/source.mkv",
        name="source.mkv",
        type="file",
        size=source_item.size,
        extension="mkv",
    )
    source_oper = Mock()
    source_oper.delete.return_value = True
    target_oper = Mock()
    target_oper.get_folder.return_value = FileItem(
        storage="remote", path="/library", name="library", type="dir"
    )
    target_oper.upload.return_value = target_item
    runner = _ImmediateStepRunner()

    result, error = TransHandler._TransHandler__execute_transfer_with_steps(
        step_runner=runner,
        fileitem=source_item,
        target_storage="remote",
        source_oper=source_oper,
        target_oper=target_oper,
        target_file=Path("/library/source.mkv"),
        transfer_type="move",
    )

    assert error == ""
    assert result == target_item
    assert [kind for _phase, kind, _payload in runner.steps] == [
        "materialize_target",
        "delete_move_source",
    ]
    assert runner.steps[0][2]["transfer_type"] == "copy"
    target_oper.upload.assert_called_once()
    source_oper.delete.assert_called_once_with(source_item)


def test_remote_to_local_transfer_creates_target_directory_before_download(tmp_path):
    """网盘到本地的下载开始前必须先创建缺失的目标目录。"""
    target_file = tmp_path / "library" / "Season 1" / "episode.mkv"
    source_item = FileItem(
        storage="alist",
        path="/downloads/episode.mkv",
        name="episode.mkv",
        type="file",
        size=5,
        extension="mkv",
    )

    def download(*, fileitem, path):
        """模拟只接受已存在本地目录的网盘下载适配器。"""
        assert fileitem is source_item
        assert path == target_file.parent
        assert path.is_dir()
        temporary_file = path / fileitem.name
        temporary_file.write_bytes(b"movie")
        return temporary_file

    source_oper = Mock()
    source_oper.download.side_effect = download

    result, error = TransHandler._TransHandler__transfer_command(
        fileitem=source_item,
        target_storage="local",
        source_oper=source_oper,
        target_oper=Mock(),
        target_file=target_file,
        transfer_type="move",
    )

    assert error == ""
    assert result is not None
    assert target_file.read_bytes() == b"movie"
    source_oper.download.assert_called_once_with(
        fileitem=source_item,
        path=target_file.parent,
    )
    source_oper.delete.assert_called_once_with(source_item)
