"""验证整理执行证据、CAS fencing 与终态结算持久化。"""

from dataclasses import replace
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.application.transfer.execution import (
    TransferExecutionCheckpoint,
    TransferExecutionCommand,
    TransferExecutionConflictError,
    TransferExecutionState,
    TransferManualReviewDecision,
    TransferOperationObservation,
    TransferOperationObservationState,
    TransferStepIntent,
    TransferStepResult,
    TransferStepState,
    build_transfer_checkpoint_fingerprint,
    build_transfer_operation_id,
)
from app.application.transfer.workflow import (
    TransferPlanCheckpoint,
    TransferPlanItem,
    TransferPlanningInput,
    TransferProviderInvocationSnapshot,
    TransferProviderReference,
)
from app.db.adapters.transfer.execution import (
    TransactionalTransferExecutionRepository,
)
from app.db.base import Base
from app.db.models.transferexecutionstep import TransferExecutionStep
from app.db.models.transferhistory import TransferHistory
from app.db.models.transferpending import TransferPending


@pytest.fixture
def execution_store():
    """构造只含整理执行相关表的独立内存数据库。"""
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


def _planning_input(task_id: str) -> TransferPlanningInput:
    """构造与测试源文件一致的持久规划输入。"""
    return TransferPlanningInput(
        source_fileitem={
            "storage": "local",
            "path": f"/{task_id}.mkv",
            "type": "file",
        },
        target_storage="local",
        target_path="/media",
        requested_transfer_type="copy",
    )


def _plan_checkpoint(task_id: str) -> TransferPlanCheckpoint:
    """构造包含一个叶子文件计划的完整宿主检查点。"""
    planning_input = _planning_input(task_id)
    return TransferPlanCheckpoint(
        planning_input=planning_input,
        target_storage="local",
        root_target_path="/media",
        final_target_path=f"/media/{task_id}.mkv",
        resolved_transfer_type="copy",
        items=(TransferPlanItem(
            sequence=0,
            source_fileitem=planning_input.source_fileitem,
            target_storage="local",
            target_path=f"/media/{task_id}.mkv",
        ),),
    )


def _plan_fingerprint(task_id: str) -> str:
    """返回测试冻结计划使用的 canonical 指纹。"""
    return build_transfer_checkpoint_fingerprint(
        _plan_checkpoint(task_id).to_payload()
    )


def _seed_pending(
        factory,
        *,
        task_id: str = "task-1",
        lease_token: str = "lease-1",
        state: str = "planned",
        with_checkpoint: bool = True,
):
    """写入一条带有效租约与合法 planning checkpoint 的待执行任务。"""
    planning_input = _planning_input(task_id)
    checkpoint = _plan_checkpoint(task_id) if with_checkpoint else None
    with factory() as session:
        session.add(TransferPending(
            task_id=task_id,
            storage="local",
            src_path=f"/{task_id}.mkv",
            created_at="2026-08-27 09:00:00",
            state=state,
            updated_at="2026-08-27 09:00:00",
            input_version=1,
            planning_input=planning_input.to_payload(),
            input_fingerprint=planning_input.fingerprint,
            checkpoint_version=checkpoint.schema_version if checkpoint else None,
            checkpoint_payload=checkpoint.to_payload() if checkpoint else None,
            planned_at="2026-08-27 09:00:00" if checkpoint else None,
            lease_owner="worker-1",
            lease_token=lease_token,
            lease_expires_at="2099-01-01 00:00:00.000000",
            heartbeat_at="2026-08-27 01:00:00.000000",
            attempt_count=1,
            execution_state="not_started",
            retry_generation=0,
            retry_count=0,
            settlement_revision=0,
        ))
        session.commit()


def _repository(factory, token_values: list[str] | None = None):
    """构造固定时钟与可预测 attempt token 的执行命令。"""
    repository = TransactionalTransferExecutionRepository(
        factory,
        local_clock=lambda: datetime(2026, 8, 27, 9, 30, 0),
        lease_clock=lambda: datetime(2026, 8, 27, 1, 30, 0, tzinfo=timezone.utc),
    )
    values = iter(token_values or ["attempt-1", "attempt-2", "attempt-3"])
    return repository, TransferExecutionCommand(
        repository,
        attempt_token_factory=lambda: next(values),
    )


def _intent(*, task_id: str = "task-1", ordinal: int = 0) -> TransferStepIntent:
    """构造稳定且可重复计算身份的测试步骤意图。"""
    planning_input = _planning_input(task_id)
    return TransferStepIntent.create(
        task_id=task_id,
        checkpoint_fingerprint=_plan_fingerprint(task_id),
        ordinal=ordinal,
        phase="transfer",
        kind="materialize_target",
        payload={
            "source": planning_input.source_fileitem,
            "target_storage": "local",
            "target_path": f"/media/{task_id}.mkv",
            "transfer_type": "copy",
        },
    )


def test_stable_operation_and_checkpoint_identities_are_canonical():
    """字段顺序不能改变 operation ID 或执行 checkpoint 指纹。"""
    first = build_transfer_operation_id(
        task_id="task",
        checkpoint_fingerprint="plan",
        ordinal=2,
        phase="scrape",
        kind="write",
        intent_payload={"b": 2, "a": 1},
    )
    second = build_transfer_operation_id(
        task_id="task",
        checkpoint_fingerprint="plan",
        ordinal=2,
        phase="scrape",
        kind="write",
        intent_payload={"a": 1, "b": 2},
    )
    assert first == second
    assert build_transfer_checkpoint_fingerprint({"b": 2, "a": 1}) == (
        build_transfer_checkpoint_fingerprint({"a": 1, "b": 2})
    )
    mutable_payload = {"path": "/original"}
    intent = TransferStepIntent.create(
        task_id="task",
        checkpoint_fingerprint="plan",
        ordinal=0,
        phase="transfer",
        kind="copy",
        payload=mutable_payload,
    )
    mutable_payload["path"] = "/mutated"
    assert intent.payload == {"path": "/original"}


@pytest.mark.parametrize(
    ("state", "with_checkpoint"),
    (("accepted", False), ("planned", False)),
)
def test_prepare_rejects_task_without_executable_plan(
        execution_store,
        state,
        with_checkpoint,
):
    """接纳态或缺失完整 checkpoint 的任务不得进入外部步骤准备。"""
    _seed_pending(
        execution_store,
        state=state,
        with_checkpoint=with_checkpoint,
    )
    _, command = _repository(execution_store)
    with pytest.raises(TransferExecutionConflictError):
        command.prepare(
            task_id="task-1",
            lease_token="lease-1",
            intent=_intent(),
        )
    with execution_store() as session:
        pending = session.scalar(select(TransferPending))
        assert pending is not None
        assert pending.execution_state == "not_started"
        assert session.scalar(select(TransferExecutionStep)) is None


def test_prepare_rejects_noncanonical_plan_fingerprint(execution_store):
    """步骤 intent 必须精确绑定当前持久计划的 canonical 指纹。"""
    _seed_pending(execution_store)
    _, command = _repository(execution_store)
    intent = TransferStepIntent.create(
        task_id="task-1",
        checkpoint_fingerprint="0" * 64,
        ordinal=0,
        phase="transfer",
        kind="materialize_target",
        payload=_intent().payload,
    )
    with pytest.raises(TransferExecutionConflictError, match="当前冻结计划指纹"):
        command.prepare(
            task_id="task-1",
            lease_token="lease-1",
            intent=intent,
        )


def test_prepare_rejects_forged_operation_id(execution_store):
    """调用方手工构造的伪 operation ID 不得绕过稳定身份计算。"""
    _seed_pending(execution_store)
    _, command = _repository(execution_store)
    valid = _intent()
    forged = TransferStepIntent(
        operation_id="f" * 64,
        checkpoint_fingerprint=valid.checkpoint_fingerprint,
        ordinal=valid.ordinal,
        phase=valid.phase,
        kind=valid.kind,
        payload=valid.payload,
    )
    with pytest.raises(TransferExecutionConflictError, match="operation ID 不可信"):
        command.prepare(
            task_id="task-1",
            lease_token="lease-1",
            intent=forged,
        )


def test_prepare_rejects_arbitrary_intent_with_known_plan_fingerprint(
        execution_store,
):
    """已知计划指纹也不能构造计划未授权的操作类型或参数。"""
    _seed_pending(execution_store)
    _, command = _repository(execution_store)
    arbitrary = TransferStepIntent.create(
        task_id="task-1",
        checkpoint_fingerprint=_plan_fingerprint("task-1"),
        ordinal=0,
        phase="transfer",
        kind="delete_move_source",
        payload={
            "source": _planning_input("task-1").source_fileitem,
            "target_storage": "local",
            "target_path": "/media/task-1.mkv",
        },
    )
    with pytest.raises(TransferExecutionConflictError, match="冻结计划导出"):
        command.prepare(
            task_id="task-1",
            lease_token="lease-1",
            intent=arbitrary,
        )


def test_prepare_rejects_noncontiguous_ordinal(execution_store):
    """新步骤只能在完整既有序列尾部连续追加。"""
    _seed_pending(execution_store)
    _, command = _repository(execution_store)
    with pytest.raises(TransferExecutionConflictError, match="连续追加"):
        command.prepare(
            task_id="task-1",
            lease_token="lease-1",
            intent=_intent(ordinal=1),
        )


def test_stage_execution_running_cas_binds_exact_plan_identity(execution_store):
    """running CAS 必须拒绝读取后已变化的准入状态或 checkpoint payload。"""
    _seed_pending(execution_store)
    checkpoint = _plan_checkpoint("task-1")
    with execution_store() as session:
        stale = TransferPending.stage_execution_running(
            session,
            task_id="task-1",
            lease_token="lease-1",
            admission_state="planned",
            checkpoint_version=checkpoint.schema_version,
            checkpoint_payload={"schema_version": checkpoint.schema_version},
            now_utc="2026-08-27 01:30:00.000000",
            updated_at="2026-08-27 09:30:00",
        )
        assert stale == 0
        current = TransferPending.stage_execution_running(
            session,
            task_id="task-1",
            lease_token="lease-1",
            admission_state="planned",
            checkpoint_version=checkpoint.schema_version,
            checkpoint_payload=checkpoint.to_payload(),
            now_utc="2026-08-27 01:30:00.000000",
            updated_at="2026-08-27 09:30:00",
        )
        assert current == 1


def test_success_path_persists_steps_and_execution_checkpoint(execution_store):
    """成功路径应保留每步证据，并提交可供唯一 durable writer 结算的检查点。"""
    _seed_pending(execution_store)
    repository, command = _repository(execution_store)
    prepared = command.prepare(
        task_id="task-1",
        lease_token="lease-1",
        intent=_intent(),
    )
    assert prepared.state is TransferStepState.PREPARED
    started = command.begin(
        task_id="task-1",
        lease_token="lease-1",
        operation_id=prepared.operation_id,
    )
    assert started.attempt_token == "attempt-1"
    succeeded = command.complete(
        task_id="task-1",
        lease_token="lease-1",
        step=started,
        result=TransferStepResult(payload={"dest_exists": True}),
    )
    checkpoint = TransferExecutionCheckpoint.create(
        payload={"outcome": "succeeded", "dest": "/media/task-1.mkv"},
        operation_ids=(succeeded.operation_id,),
    )
    snapshot = command.checkpoint(
        task_id="task-1",
        lease_token="lease-1",
        checkpoint=checkpoint,
    )
    assert snapshot.state is TransferExecutionState.SETTLING
    with execution_store() as session:
        pending = session.scalar(select(TransferPending))
        assert pending is not None
        assert pending.execution_fingerprint == checkpoint.fingerprint
        step = session.scalar(select(TransferExecutionStep))
        assert step is not None and step.state == "succeeded"


def test_provider_predecessor_remains_owned_after_host_plan_promotion(
        execution_store,
):
    """provider 回退升级宿主计划后，序号零证据仍应被严格重建并纳入 checkpoint。"""
    task_id = "task-1"
    planning_input = _planning_input(task_id)
    provider = TransferProviderReference(
        plugin_id="provider-a",
        plugin_name="Provider A",
    )
    invocation = TransferProviderInvocationSnapshot(
        fileitem=planning_input.source_fileitem,
        meta={"title": "Movie"},
        meta_kind="MetaVideo",
        mediainfo={"title": "Movie"},
        mediainfo_kind="MediaInfo",
    )
    provider_checkpoint = TransferPlanCheckpoint(
        planning_input=planning_input,
        target_storage="",
        root_target_path="",
        final_target_path="",
        resolved_transfer_type="",
        items=(),
        resolved_meta=invocation.meta,
        resolved_meta_kind=invocation.meta_kind,
        resolved_mediainfo=invocation.mediainfo,
        resolved_mediainfo_kind=invocation.mediainfo_kind,
        legacy_transfer_providers=(provider,),
        provider_invocation=invocation,
    )
    _seed_pending(execution_store)
    with execution_store() as session:
        pending = session.scalar(select(TransferPending))
        assert pending is not None
        pending.state = "provider_pending"
        pending.checkpoint_payload = provider_checkpoint.to_payload()
        session.commit()
    _, command = _repository(execution_store)
    provider_intent = TransferStepIntent.create(
        task_id=task_id,
        checkpoint_fingerprint=build_transfer_checkpoint_fingerprint(
            provider_checkpoint.to_payload()
        ),
        ordinal=0,
        phase="provider",
        kind="legacy_transfer_provider_sequence",
        payload={
            "providers": [provider.to_payload()],
            "invocation": invocation.to_payload(),
        },
    )
    prepared = command.prepare(
        task_id=task_id,
        lease_token="lease-1",
        intent=provider_intent,
    )
    started = command.begin(
        task_id=task_id,
        lease_token="lease-1",
        operation_id=prepared.operation_id,
    )
    provider_succeeded = command.complete(
        task_id=task_id,
        lease_token="lease-1",
        step=started,
        result=TransferStepResult(payload={"handled": False}),
    )
    promoted_checkpoint = replace(
        _plan_checkpoint(task_id),
        pre_execution_cleanup_completed=True,
    )
    with execution_store() as session:
        pending = session.scalar(select(TransferPending))
        assert pending is not None
        pending.state = "planned"
        pending.checkpoint_payload = promoted_checkpoint.to_payload()
        session.commit()
    promoted_fingerprint = build_transfer_checkpoint_fingerprint(
        promoted_checkpoint.to_payload()
    )
    host_intent = TransferStepIntent.create(
        task_id=task_id,
        checkpoint_fingerprint=promoted_fingerprint,
        ordinal=1,
        phase="transfer",
        kind="materialize_target",
        payload=_intent().payload,
    )
    host_prepared = command.prepare(
        task_id=task_id,
        lease_token="lease-1",
        intent=host_intent,
    )
    host_started = command.begin(
        task_id=task_id,
        lease_token="lease-1",
        operation_id=host_prepared.operation_id,
    )
    host_succeeded = command.complete(
        task_id=task_id,
        lease_token="lease-1",
        step=host_started,
        result=TransferStepResult(payload={"dest_exists": True}),
    )
    execution_checkpoint = TransferExecutionCheckpoint.create(
        payload={"outcome": "succeeded", "dest": "/media/task-1.mkv"},
        operation_ids=(
            provider_succeeded.operation_id,
            host_succeeded.operation_id,
        ),
    )
    snapshot = command.checkpoint(
        task_id=task_id,
        lease_token="lease-1",
        checkpoint=execution_checkpoint,
    )
    assert snapshot.state is TransferExecutionState.SETTLING
    assert tuple(step.ordinal for step in snapshot.steps) == (0, 1)


def test_checkpoint_rejects_operation_ids_out_of_ordinal_order(execution_store):
    """执行检查点必须按严格 ordinal 保存操作身份，集合相同也不能乱序。"""
    _seed_pending(execution_store)
    _, command = _repository(execution_store)
    completed = []
    for ordinal in range(2):
        prepared = command.prepare(
            task_id="task-1",
            lease_token="lease-1",
            intent=_intent(ordinal=ordinal),
        )
        started = command.begin(
            task_id="task-1",
            lease_token="lease-1",
            operation_id=prepared.operation_id,
        )
        completed.append(command.complete(
            task_id="task-1",
            lease_token="lease-1",
            step=started,
            result=TransferStepResult(payload={"ordinal": ordinal}),
        ))
    checkpoint = TransferExecutionCheckpoint.create(
        payload={"outcome": "succeeded", "dest": "/media/task-1.mkv"},
        operation_ids=tuple(step.operation_id for step in reversed(completed)),
    )
    with pytest.raises(TransferExecutionConflictError, match="步骤顺序"):
        command.checkpoint(
            task_id="task-1",
            lease_token="lease-1",
            checkpoint=checkpoint,
        )


def test_checkpoint_rejects_corrupted_persisted_operation_id(execution_store):
    """checkpoint 前必须重新计算每个持久步骤的 operation ID。"""
    _seed_pending(execution_store)
    _, command = _repository(execution_store)
    prepared = command.prepare(
        task_id="task-1",
        lease_token="lease-1",
        intent=_intent(),
    )
    started = command.begin(
        task_id="task-1",
        lease_token="lease-1",
        operation_id=prepared.operation_id,
    )
    command.complete(
        task_id="task-1",
        lease_token="lease-1",
        step=started,
        result=TransferStepResult(payload={"dest_exists": True}),
    )
    with execution_store() as session:
        step = session.scalar(select(TransferExecutionStep))
        assert step is not None
        step.operation_id = "e" * 64
        session.commit()
    checkpoint = TransferExecutionCheckpoint.create(
        payload={"outcome": "succeeded", "dest": "/media/task-1.mkv"},
        operation_ids=("e" * 64,),
    )
    with pytest.raises(TransferExecutionConflictError, match="冻结意图"):
        command.checkpoint(
            task_id="task-1",
            lease_token="lease-1",
            checkpoint=checkpoint,
        )


def test_checkpoint_rejects_noncontiguous_persisted_ordinals(execution_store):
    """checkpoint 前必须拒绝缺口或从非零开始的持久步骤序列。"""
    _seed_pending(execution_store)
    _, command = _repository(execution_store)
    prepared = command.prepare(
        task_id="task-1",
        lease_token="lease-1",
        intent=_intent(),
    )
    started = command.begin(
        task_id="task-1",
        lease_token="lease-1",
        operation_id=prepared.operation_id,
    )
    command.complete(
        task_id="task-1",
        lease_token="lease-1",
        step=started,
        result=TransferStepResult(payload={"dest_exists": True}),
    )
    with execution_store() as session:
        step = session.scalar(select(TransferExecutionStep))
        assert step is not None
        step.ordinal = 2
        step.operation_id = build_transfer_operation_id(
            task_id=step.task_id,
            checkpoint_fingerprint=step.checkpoint_fingerprint,
            ordinal=step.ordinal,
            phase=step.phase,
            kind=step.kind,
            intent_payload=step.intent_payload,
        )
        corrupted_operation_id = step.operation_id
        session.commit()
    checkpoint = TransferExecutionCheckpoint.create(
        payload={"outcome": "succeeded", "dest": "/media/task-1.mkv"},
        operation_ids=(corrupted_operation_id,),
    )
    with pytest.raises(TransferExecutionConflictError, match="全局序号不连续"):
        command.checkpoint(
            task_id="task-1",
            lease_token="lease-1",
            checkpoint=checkpoint,
        )


def test_retry_wait_resumes_same_failed_operation_with_new_attempt(execution_store):
    """到期重试必须复用 operation ID、保留失败证据并轮换 attempt token。"""
    _seed_pending(execution_store)
    repository, command = _repository(execution_store)
    prepared = command.prepare(
        task_id="task-1", lease_token="lease-1", intent=_intent()
    )
    started = command.begin(
        task_id="task-1",
        lease_token="lease-1",
        operation_id=prepared.operation_id,
    )
    deferred = command.defer(
        task_id="task-1",
        lease_token="lease-1",
        step=started,
        error="destination unavailable",
        retry_due_at="2026-08-27 01:30:01.000000",
        evidence=TransferStepResult(payload={"applied": False}),
    )
    assert deferred.state is TransferExecutionState.RETRY_WAIT
    assert deferred.retry_generation == 1
    with execution_store() as session:
        claimed = TransferPending.claim_task(
            session,
            task_id="task-1",
            states=("planned",),
            owner_id="worker-2",
            lease_token="lease-2",
            now_time="2026-08-27 01:30:02.000000",
            lease_expires_at="2099-01-01 00:00:00.000000",
            updated_at="2026-08-27 09:30:02",
        )
        session.commit()
        assert claimed == 1
    resumed = command.resume_failed(
        task_id="task-1",
        lease_token="lease-2",
        step=deferred.steps[0],
    )
    assert resumed.operation_id == prepared.operation_id
    assert resumed.attempt_token == "attempt-2"
    assert resumed.attempt_count == 2
    assert resumed.result == TransferStepResult(payload={"applied": False})
    assert resumed.last_error == "destination unavailable"


def test_orphan_started_requires_observation_before_attempt_rotation(execution_store):
    """遗留 STARTED 只能凭 NOT_APPLIED 证据轮换 attempt，旧 attempt 随即失效。"""
    _seed_pending(execution_store)
    repository, command = _repository(execution_store)
    prepared = command.prepare(
        task_id="task-1", lease_token="lease-1", intent=_intent()
    )
    started = command.begin(
        task_id="task-1",
        lease_token="lease-1",
        operation_id=prepared.operation_id,
    )
    observation = TransferOperationObservation(
        state=TransferOperationObservationState.NOT_APPLIED,
        evidence=TransferStepResult(payload={"dest_exists": False}),
    )
    restarted = command.restart_after_not_applied(
        task_id="task-1",
        lease_token="lease-1",
        step=started,
        evidence=observation.evidence,
    )
    assert restarted.attempt_token == "attempt-2"
    assert restarted.attempt_count == 2
    with pytest.raises(TransferExecutionConflictError):
        repository.complete_step(
            task_id="task-1",
            lease_token="lease-1",
            operation_id=started.operation_id,
            attempt_token="attempt-1",
            result=TransferStepResult(payload={"stale": True}),
        )


def test_zero_side_effect_checkpoint_is_vacuously_complete(execution_store):
    """纯策略拒绝可在没有步骤行时提交带 skip_reason 的确定执行结果。"""
    _seed_pending(execution_store)
    _, command = _repository(execution_store)
    checkpoint = TransferExecutionCheckpoint.create(
        payload={"outcome": "failed", "preview": True, "accepted": False},
        operation_ids=(),
        skip_reason="preview",
    )
    snapshot = command.checkpoint(
        task_id="task-1",
        lease_token="lease-1",
        checkpoint=checkpoint,
    )
    assert snapshot.state is TransferExecutionState.SETTLING
    assert snapshot.checkpoint == checkpoint
    assert snapshot.steps == ()


def test_exhausted_step_builds_failure_checkpoint_and_keeps_lease(execution_store):
    """预算耗尽应原子建立失败结算检查点，并为 durable writer 保留 lease。"""
    _seed_pending(execution_store)
    _, command = _repository(execution_store)
    prepared = command.prepare(
        task_id="task-1", lease_token="lease-1", intent=_intent()
    )
    started = command.begin(
        task_id="task-1",
        lease_token="lease-1",
        operation_id=prepared.operation_id,
    )
    snapshot = command.exhaust(
        task_id="task-1",
        lease_token="lease-1",
        step=started,
        error="retry budget exhausted",
        evidence=TransferStepResult(payload={"applied": False}),
    )
    assert snapshot.state is TransferExecutionState.SETTLING
    assert snapshot.checkpoint is not None
    assert snapshot.checkpoint.payload["outcome"] == "failed"
    assert snapshot.checkpoint.payload["error"] == "retry budget exhausted"
    assert snapshot.checkpoint.operation_ids == (started.operation_id,)
    assert snapshot.steps[0].state is TransferStepState.FAILED
    with execution_store() as session:
        pending = session.scalar(select(TransferPending))
        assert pending is not None
        assert pending.lease_token == "lease-1"


def test_user_retry_is_single_generation_and_rejects_manual_review(execution_store):
    """FAILED 用户重试只递增一次世代，重复请求幂等且人工复核必须拒绝。"""
    _seed_pending(execution_store)
    with execution_store() as session:
        pending = session.scalar(select(TransferPending))
        assert pending is not None
        pending.execution_state = "failed"
        pending.lease_owner = None
        pending.lease_token = None
        pending.lease_expires_at = None
        pending.terminal_history_id = 42
        pending.retry_count = 3
        session.commit()
    _, command = _repository(execution_store)
    first = command.request_retry(
        task_id="task-1",
        reason="用户确认目标未落地",
        requested_by="admin",
    )
    repeated = command.request_retry(
        task_id="task-1",
        reason="重复点击",
        requested_by="admin",
    )
    assert first.accepted and repeated.accepted
    assert first.retry_generation == repeated.retry_generation == 1
    with execution_store() as session:
        pending = session.scalar(select(TransferPending))
        assert pending is not None
        assert pending.execution_state == "retry_wait"
        assert pending.retry_count == 3
        assert pending.terminal_history_id == 42
        assert pending.retry_reason == "用户确认目标未落地"
        assert pending.retry_requested_by == "admin"
        pending.execution_state = "manual_review"
        session.commit()
    rejected = command.request_retry(
        task_id="task-1",
        reason="强制重试",
        requested_by="admin",
    )
    assert not rejected.accepted
    assert rejected.state is TransferExecutionState.MANUAL_REVIEW
    assert "人工" in rejected.message


def test_manual_not_applied_decision_is_audited_and_schedules_same_step(
        execution_store,
) -> None:
    """人工判定未发生应无 lease 地恢复 FAILED，并只交给唯一调度器。"""
    _seed_pending(execution_store)
    _, command = _repository(execution_store)
    prepared = command.prepare(
        task_id="task-1", lease_token="lease-1", intent=_intent()
    )
    started = command.begin(
        task_id="task-1",
        lease_token="lease-1",
        operation_id=prepared.operation_id,
    )
    manual = command.manual_review(
        task_id="task-1",
        lease_token="lease-1",
        step=started,
        error="external result unknown",
    )
    assert manual.state is TransferExecutionState.MANUAL_REVIEW
    resolved = command.resolve_manual_review(
        task_id="task-1",
        operation_id=started.operation_id,
        decision=TransferManualReviewDecision.NOT_APPLIED,
        actor="admin",
        reason="目标与临时文件均不存在",
        result=TransferStepResult(payload={"dest_exists": False}),
    )
    assert resolved.state is TransferExecutionState.RETRY_WAIT
    assert resolved.review_revision == 1
    assert resolved.step.state is TransferStepState.FAILED
    with execution_store() as session:
        pending = session.scalar(select(TransferPending))
        assert pending is not None
        assert pending.lease_token is None
        assert pending.reviewed_by == "admin"
        assert pending.review_decision == "not_applied"
        assert pending.review_reason == "目标与临时文件均不存在"
    with pytest.raises(TransferExecutionConflictError):
        command.resolve_manual_review(
            task_id="task-1",
            operation_id=started.operation_id,
            decision=TransferManualReviewDecision.NOT_APPLIED,
            actor="admin",
            reason="重复判定",
        )


def test_manual_applied_requires_result_and_failed_decision_is_rejected(
        execution_store,
) -> None:
    """人工判定已发生必须带结果证据，FAILED 不得绕过 lease durable 结算。"""
    _seed_pending(execution_store)
    _, command = _repository(execution_store)
    prepared = command.prepare(
        task_id="task-1", lease_token="lease-1", intent=_intent()
    )
    started = command.begin(
        task_id="task-1",
        lease_token="lease-1",
        operation_id=prepared.operation_id,
    )
    command.manual_review(
        task_id="task-1",
        lease_token="lease-1",
        step=started,
        error="external result unknown",
    )
    with pytest.raises(ValueError, match="结果证据"):
        command.resolve_manual_review(
            task_id="task-1",
            operation_id=started.operation_id,
            decision=TransferManualReviewDecision.APPLIED,
            actor="admin",
            reason="已确认目标存在",
        )
    with pytest.raises(TransferExecutionConflictError, match="durable"):
        command.resolve_manual_review(
            task_id="task-1",
            operation_id=started.operation_id,
            decision=TransferManualReviewDecision.FAILED,
            actor="admin",
            reason="确认失败",
        )
    resolved = command.resolve_manual_review(
        task_id="task-1",
        operation_id=started.operation_id,
        decision=TransferManualReviewDecision.APPLIED,
        actor="admin",
        reason="目标摘要匹配",
        result=TransferStepResult(payload={"dest_exists": True, "hash_match": True}),
    )
    assert resolved.step.state is TransferStepState.SUCCEEDED
    assert resolved.step.result == TransferStepResult(
        payload={"dest_exists": True, "hash_match": True}
    )
