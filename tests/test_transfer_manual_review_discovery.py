"""验证 durable 人工复核从未知证据到调度恢复的可发现闭环。"""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.api.endpoints import transfer as transfer_endpoint
from app.application.transfer.execution import (
    TransferExecutionCommand,
    TransferExecutionState,
    TransferManualReviewQuery,
    TransferStepIntent,
    TransferStepResult,
)
from app.application.transfer.workflow import (
    TransferPlanCheckpoint,
    TransferPlanItem,
    TransferPlanningInput,
)
from app.db.adapters.transfer.execution import (
    TransactionalTransferExecutionRepository,
)
from app.db.base import Base
from app.db.models.transferexecutionstep import TransferExecutionStep
from app.db.models.transferhistory import TransferHistory
from app.db.models.transferpending import TransferPending
from app.schemas.transfer import TransferManualReviewRequest


@pytest.fixture
def review_store():
    """构造隔离的 durable 整理人工复核数据库。"""
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
    try:
        yield factory
    finally:
        engine.dispose()


def _repository(factory) -> TransactionalTransferExecutionRepository:
    """构造使用确定时钟的整理执行仓储。"""
    return TransactionalTransferExecutionRepository(
        factory,
        local_clock=lambda: datetime(2026, 8, 27, 9, 30, 0),
        lease_clock=lambda: datetime(2026, 8, 27, 1, 30, 0, tzinfo=timezone.utc),
    )


def _put_in_manual_review(factory, *, task_id: str) -> tuple[
    TransactionalTransferExecutionRepository,
    str,
]:
    """建立一个外部结果 UNKNOWN 且已释放租约的人工复核任务。"""
    source_path = f"/downloads/{task_id}.mkv"
    target_path = f"/library/{task_id}.mkv"
    planning_input = TransferPlanningInput(
        source_fileitem={
            "storage": "local",
            "path": source_path,
            "type": "file",
        },
        target_storage="local",
        target_path=target_path,
        requested_transfer_type="copy",
    )
    checkpoint = TransferPlanCheckpoint(
        planning_input=planning_input,
        target_storage="local",
        root_target_path="/library",
        final_target_path=target_path,
        resolved_transfer_type="copy",
        items=(TransferPlanItem(
            sequence=0,
            source_fileitem=planning_input.source_fileitem,
            target_storage="local",
            target_path=target_path,
        ),),
    )
    with factory() as session:
        session.add(TransferPending(
            task_id=task_id,
            storage="local",
            src_path=source_path,
            created_at="2026-08-27 09:00:00",
            state="planned",
            updated_at="2026-08-27 09:00:00",
            input_version=1,
            planning_input=planning_input.to_payload(),
            input_fingerprint=planning_input.fingerprint,
            checkpoint_version=checkpoint.schema_version,
            checkpoint_payload=checkpoint.to_payload(),
            planned_at="2026-08-27 09:00:00",
            lease_owner="worker-secret",
            lease_token=f"lease-{task_id}",
            lease_expires_at="2099-01-01 00:00:00.000000",
            heartbeat_at="2026-08-27 01:00:00.000000",
            attempt_count=1,
            execution_state="not_started",
            retry_generation=0,
            retry_count=0,
            settlement_revision=0,
        ))
        session.commit()
    repository = _repository(factory)
    command = TransferExecutionCommand(
        repository,
        attempt_token_factory=lambda: f"attempt-{task_id}",
    )
    intent = TransferStepIntent.create(
        task_id=task_id,
        checkpoint_fingerprint=checkpoint.fingerprint,
        ordinal=0,
        phase="transfer",
        kind="materialize_target",
        payload={
            "source": planning_input.source_fileitem,
            "target_storage": "local",
            "target_path": target_path,
            "transfer_type": "copy",
        },
    )
    prepared = command.prepare(
        task_id=task_id,
        lease_token=f"lease-{task_id}",
        intent=intent,
    )
    started = command.begin(
        task_id=task_id,
        lease_token=f"lease-{task_id}",
        operation_id=prepared.operation_id,
    )
    snapshot = command.manual_review(
        task_id=task_id,
        lease_token=f"lease-{task_id}",
        step=started,
        error="external result unknown",
        evidence=TransferStepResult(payload={
            "observation": "unknown",
            "target_exists": True,
        }),
    )
    assert snapshot.state is TransferExecutionState.MANUAL_REVIEW
    return repository, started.operation_id


def test_manual_review_list_is_database_paginated(review_store) -> None:
    """待复核任务应按稳定顺序在数据库分页，不混入普通任务。"""
    repository, _ = _put_in_manual_review(review_store, task_id="task-1")
    _put_in_manual_review(review_store, task_id="task-2")

    first = repository.list_manual_reviews(
        state=TransferExecutionState.MANUAL_REVIEW,
        page=1,
        page_size=1,
    )
    second = repository.list_manual_reviews(
        state=TransferExecutionState.MANUAL_REVIEW,
        page=2,
        page_size=1,
    )

    assert first.total == second.total == 2
    assert len(first.items) == len(second.items) == 1
    assert first.items[0].task_id != second.items[0].task_id
    with pytest.raises(ValueError, match="不支持状态"):
        TransferManualReviewQuery(repository).list(
            state=TransferExecutionState.RUNNING,
        )


@pytest.mark.parametrize(
    ("decision", "result_payload", "expected_step_state"),
    [
        ("applied", {"target_exists": True, "hash_match": True}, "succeeded"),
        ("not_applied", None, "failed"),
    ],
)
def test_unknown_manual_review_is_discoverable_and_resumes_via_api(
    monkeypatch,
    review_store,
    decision: str,
    result_payload: dict[str, bool] | None,
    expected_step_state: str,
) -> None:
    """UNKNOWN 任务应可发现，人工判定后进入唯一 retry_wait 恢复路径。"""
    repository, operation_id = _put_in_manual_review(
        review_store,
        task_id=f"task-{decision}",
    )
    listed = transfer_endpoint.list_transfer_manual_reviews(
        state_filter="manual_review",
        page=1,
        page_size=10,
        current_user=object(),
        repository=repository,
    )
    assert listed.data is not None
    assert listed.data.total == 1
    discovered = listed.data.items[0]
    assert discovered.task_id == f"task-{decision}"
    assert discovered.source.model_dump() == {
        "storage": "local",
        "path": f"/downloads/task-{decision}.mkv",
    }
    assert discovered.step.operation_id == operation_id
    assert discovered.step.kind == "materialize_target"
    assert (
        discovered.step.intent["target_path"]
        == f"/library/task-{decision}.mkv"
    )
    assert discovered.step.evidence == {
        "observation": "unknown",
        "target_exists": True,
    }
    assert discovered.step.error == "external result unknown"
    assert discovered.review_revision == 0
    public_json = listed.model_dump_json()
    assert "worker-secret" not in public_json
    assert "lease-" not in public_json
    assert "attempt-" not in public_json

    detail = transfer_endpoint.get_transfer_manual_review(
        task_id=f"task-{decision}",
        current_user=object(),
        repository=repository,
    )
    assert detail.data == discovered

    resolved = transfer_endpoint.resolve_transfer_manual_review(
        task_id=f"task-{decision}",
        review=TransferManualReviewRequest(
            operation_id=operation_id,
            decision=decision,
            reason=f"reviewed-{decision}",
            result_payload=result_payload,
        ),
        current_user=SimpleNamespace(name="admin"),
        repository=repository,
    )
    assert resolved.data is not None
    assert resolved.data.state == "retry_wait"
    assert resolved.data.review_revision == 1

    waiting = transfer_endpoint.get_transfer_manual_review(
        task_id=f"task-{decision}",
        current_user=object(),
        repository=repository,
    )
    assert waiting.data is not None
    assert waiting.data.state == "retry_wait"
    assert waiting.data.review_revision == 1
    retry_page = transfer_endpoint.list_transfer_manual_reviews(
        state_filter="retry_wait",
        page=1,
        page_size=10,
        current_user=object(),
        repository=repository,
    )
    assert retry_page.data is not None
    assert [item.task_id for item in retry_page.data.items] == [f"task-{decision}"]

    snapshot = repository.get_snapshot(task_id=f"task-{decision}")
    assert snapshot is not None
    assert snapshot.state is TransferExecutionState.RETRY_WAIT
    assert snapshot.retry_due_at is not None
    assert snapshot.steps[0].state.value == expected_step_state
    with review_store() as session:
        pending = session.scalar(select(TransferPending))
        assert pending is not None
        assert pending.lease_owner is None
        assert pending.lease_token is None
        assert pending.retry_generation == 1
