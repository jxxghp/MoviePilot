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
)
from app.chain import transfer as transfer_chain_module
from app.db.adapters.transfer.execution import (
    TransactionalTransferExecutionRepository,
)
from app.db.base import Base
from app.db.models.transferexecutionstep import TransferExecutionStep
from app.db.models.transferhistory import TransferHistory
from app.db.models.transferpending import TransferPending
from app.modules.filemanager.transhandler import TransHandler
from app.schemas.workflow import FileItem


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
    with factory() as session:
        session.add(TransferPending(
            task_id="task-runner",
            storage="local",
            src_path="/source.mkv",
            created_at="2026-08-27 09:00:00",
            state="planned",
            updated_at="2026-08-27 09:00:00",
            input_version=1,
            planning_input={"schema_version": 1},
            input_fingerprint="input",
            checkpoint_version=1,
            checkpoint_payload={"schema_version": 1},
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
        checkpoint_fingerprint="plan",
        repository=repository,
    )


def test_runner_replay_returns_persisted_result_without_repeating_side_effect(
        execution_repository,
):
    """成功步骤重放只能回读结果，不能再次调用外部执行函数。"""
    calls = []
    first = _runner(execution_repository).run(
        phase="transfer",
        kind="copy",
        payload={"source": "/source.mkv", "target": "/target.mkv"},
        execute=lambda: calls.append("executed") or TransferStepResult(
            payload={"item": {"path": "/target.mkv"}}
        ),
        observe=lambda: pytest.fail("新步骤不应执行恢复探测"),
    )
    second = _runner(execution_repository).run(
        phase="transfer",
        kind="copy",
        payload={"source": "/source.mkv", "target": "/target.mkv"},
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
            checkpoint_fingerprint="plan",
            ordinal=0,
            phase="provider",
            kind="opaque",
            payload={"provider": "legacy"},
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
            phase="provider",
            kind="opaque",
            payload={"provider": "legacy"},
            execute=lambda: pytest.fail("未知遗留步骤不得重放"),
            observe=lambda: TransferOperationObservation(
                state=TransferOperationObservationState.UNKNOWN,
                evidence=TransferStepResult(payload={"receipt": None}),
            ),
        )
    snapshot = execution_repository.get_snapshot(task_id="task-runner")
    assert snapshot is not None
    assert snapshot.state is TransferExecutionState.MANUAL_REVIEW


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
