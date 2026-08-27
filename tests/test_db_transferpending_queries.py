"""
待整理登记表的查询行为。

这张表是「挂载挂死后重启不漏件」的唯一依据：登记去重、回放顺序、终态注销三件事
任何一件出偏差，都直接表现为文件被漏整理或被重复整理，而不是一个可见的报错。
因此这里对着真实数据库断言查回的内容，而不是断言调用了什么。
"""
import inspect

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.application.transfer.workflow import TransferPlanningInput
from app.db import base as db_base
from app.db.adapters.transfer.admission import TransactionalTransferAdmissionRepository
from app.db.models.transferexecutionstep import TransferExecutionStep
from app.db.models.transferhistory import TransferHistory
from app.db.models.transferpending import TransferPending
from app.db.oper.transferpending import TransferPendingOper


@pytest.fixture(autouse=True)
def _track(db):
    """把待整理表纳入用例级回收。"""
    db.watermark(TransferPending)
    db.watermark(TransferExecutionStep)


def _leased_pending(
        *,
        task_id: str,
        execution_state: str,
        admission_state: str = "accepted",
) -> TransferPending:
    """构造持有有效租约且其余执行证据为空的 pending。"""
    return TransferPending(
        task_id=task_id,
        storage="local",
        src_path=f"/mnt/{task_id}.mkv",
        state=admission_state,
        created_at="2026-08-27 10:00:00",
        updated_at="2026-08-27 10:00:00",
        input_version=1,
        planning_input={"schema_version": 1},
        input_fingerprint="input",
        lease_owner="worker",
        lease_token=f"lease-{task_id}",
        lease_expires_at="2099-01-01 00:00:00.000000",
        heartbeat_at="2026-08-27 10:00:00.000000",
        attempt_count=1,
        execution_state=execution_state,
        retry_generation=0,
        retry_count=0,
        settlement_revision=0,
    )


def _planning_input(path: str) -> TransferPlanningInput:
    """构造准入仓储要求的真实版本化规划输入。"""
    return TransferPlanningInput(
        source_fileitem={
            "storage": "local",
            "path": path,
            "type": "file",
            "name": path.rsplit("/", 1)[-1],
        },
        meta=None,
        mediainfo=None,
    )


def _planning_fields(path: str) -> dict[str, object]:
    """返回 direct model/Oper 准入所需的显式版本化字段。"""
    planning_input = _planning_input(path)
    return {
        "input_version": planning_input.schema_version,
        "planning_input": planning_input.to_payload(),
        "input_fingerprint": planning_input.fingerprint,
    }


def test_stage_admit_is_idempotent_and_keeps_stable_task_id(db):
    """显式准入重复执行时必须复用首个稳定任务标识。"""
    first = TransferPending.stage_admit(
        db.session,
        task_id="task-first",
        storage="local",
        src_path="/mnt/durable.mkv",
        state="accepted",
        now_time="2026-08-27 10:00:00",
        **_planning_fields("/mnt/durable.mkv"),
    )
    second = TransferPending.stage_admit(
        db.session,
        task_id="task-second",
        storage="local",
        src_path="/mnt/durable.mkv",
        state="accepted",
        now_time="2026-08-27 11:00:00",
        **_planning_fields("/mnt/durable.mkv"),
    )

    assert first is second
    assert second.task_id == "task-first"
    assert second.updated_at == "2026-08-27 10:00:00"


def test_unfenced_legacy_mutation_apis_are_absent() -> None:
    """持久整理表不得重新暴露绕过稳定任务身份和租约的旧写入口。"""
    for owner in (TransferPending, TransferPendingOper):
        for method_name in (
                "register",
                "discard",
                "list_all",
                "list_by_state",
                "list_by_states",
                "clear",
        ):
            assert not hasattr(owner, method_name)

    enqueue_failure_source = inspect.getsource(
        TransferPending.record_enqueue_failure
    )
    assert "cls.lease_token.is_(None)" in enqueue_failure_source
    claimable_source = inspect.getsource(
        TransferPending.list_claimable_candidates
    )
    assert "after_cursor" in claimable_source
    assert ".not_in(" not in claimable_source


def test_state_queries_and_failure_record_share_stable_identity(db):
    """状态查询与失败留痕应共享同一稳定身份。"""
    TransferPending.stage_admit(
        db.session,
        task_id="task-accepted",
        storage="local",
        src_path="/mnt/accepted.mkv",
        state="accepted",
        now_time="2026-08-27 10:00:00",
        **_planning_fields("/mnt/accepted.mkv"),
    )
    other_fields = _planning_fields("/mnt/other.mkv")
    db.add(TransferPending(
        task_id="task-other",
        storage="local",
        src_path="/mnt/other.mkv",
        state="other",
        created_at="2026-08-27 10:00:01",
        updated_at="2026-08-27 10:00:01",
        **other_fields,
    ))
    db.session.flush()

    accepted = TransferPending.get_by_task_id(
        db.session,
        task_id="task-accepted",
    )
    assert accepted.state == "accepted"
    assert TransferPending.record_enqueue_failure(
        db.session,
        task_id="task-accepted",
        error="queue full",
        now_time="2026-08-27 10:01:00",
    ) == 1
    db.session.expire_all()
    failed = TransferPending.get_by_identity(
        db.session,
        storage="local",
        src_path="/mnt/accepted.mkv",
    )
    assert failed.last_error == "queue full"
    assert failed.updated_at == "2026-08-27 10:01:00"


def test_oper_staging_reuses_explicit_write_session(db, monkeypatch):
    """Oper 的新暂存入口必须服从调用方 Session，不得隐式提交。"""
    monkeypatch.setattr(
        db_base,
        "run_sync_transaction",
        lambda _operation: (_ for _ in ()).throw(
            AssertionError("不应创建额外同步事务")
        ),
    )
    oper = TransferPendingOper(db.session)

    pending = oper.stage_admit(
        task_id="task-explicit",
        storage="local",
        src_path="/mnt/explicit-stage.mkv",
        state="accepted",
        now_time="2026-08-27 10:00:00",
        **_planning_fields("/mnt/explicit-stage.mkv"),
    )
    assert pending.task_id == "task-explicit"
    assert oper.get_by_task_id(task_id="task-explicit").state == "accepted"
    assert oper.stage_record_enqueue_failure(
        task_id="task-explicit",
        error="queue full",
        now_time="2026-08-27 10:01:00",
    ) == 1
    assert oper.get_by_task_id(task_id="task-explicit").last_error == "queue full"


def test_transactional_repository_commits_frozen_projections(tmp_path):
    """适配器应独立提交 UoW，并在会话关闭前冻结应用 DTO。"""
    engine = create_engine(f"sqlite:///{tmp_path / 'transfer.db'}")
    TransferHistory.__table__.create(engine)
    TransferPending.__table__.create(engine)
    TransferExecutionStep.__table__.create(engine)
    factory = sessionmaker(bind=engine)
    repository = TransactionalTransferAdmissionRepository(factory)

    admitted = repository.admit(
        storage="local",
        src_path="/mnt/repository.mkv",
        planning_input=_planning_input("/mnt/repository.mkv"),
    )
    repeated = repository.admit(
        storage="local",
        src_path="/mnt/repository.mkv",
        planning_input=_planning_input("/mnt/repository.mkv"),
    )
    assert repeated == admitted
    assert admitted.task_id
    assert admitted.state == "accepted"

    repository.record_enqueue_failure(
        task_id=admitted.task_id,
        error="queue full",
    )
    with factory() as session:
        failed = session.execute(
            select(TransferPending).where(
                TransferPending.task_id == admitted.task_id
            )
        ).scalar_one()
        assert failed.last_error == "queue full"
    claimed = repository.claim_task(
        task_id=admitted.task_id,
        owner_id="repository-test-worker",
        lease_seconds=60,
    )
    assert claimed is not None
    assert repository.abandon_unstarted(
        task_id=admitted.task_id,
        lease_token=claimed.lease_token,
    ) == 1
    with factory() as session:
        assert session.execute(
            select(TransferPending).where(
                TransferPending.task_id == admitted.task_id
            )
        ).scalar_one_or_none() is None
    engine.dispose()


@pytest.mark.parametrize(
    ("execution_state", "expected_deleted"),
    [
        ("not_started", 1),
        ("running", 0),
        ("retry_wait", 0),
        ("settling", 0),
        ("failed", 0),
        ("manual_review", 0),
    ],
)
def test_abandon_unstarted_allows_only_pristine_execution_state(
        db,
        execution_state,
        expected_deleted,
) -> None:
    """缺失源注销必须拒绝所有已开始、待重试、结算和人工状态。"""
    task_id = f"abandon-{execution_state}"
    db.add(_leased_pending(task_id=task_id, execution_state=execution_state))
    db.session.flush()

    deleted = TransferPending.abandon_unstarted(
        db.session,
        task_id=task_id,
        lease_token=f"lease-{task_id}",
        now_time="2026-08-27 10:01:00.000000",
    )
    db.session.flush()

    assert deleted == expected_deleted
    remaining = db.session.execute(
        select(TransferPending).where(TransferPending.task_id == task_id)
    ).scalar_one_or_none()
    assert (remaining is None) is bool(expected_deleted)


@pytest.mark.parametrize("admission_state", ["planned", "provider_pending"])
def test_abandon_unstarted_rejects_nonaccepted_admission_state(
        db,
        admission_state,
) -> None:
    """已进入 provider 或计划态的任务即使无步骤也不得按缺失源删除。"""
    task_id = f"abandon-{admission_state}"
    db.add(_leased_pending(
        task_id=task_id,
        execution_state="not_started",
        admission_state=admission_state,
    ))

    deleted = TransferPending.abandon_unstarted(
        db.session,
        task_id=task_id,
        lease_token=f"lease-{task_id}",
        now_time="2026-08-27 10:01:00.000000",
    )

    assert deleted == 0


def test_abandon_unstarted_rejects_task_with_any_step_evidence(db) -> None:
    """即使聚合状态尚未推进，已落库步骤也必须阻止删除 pending。"""
    task_id = "abandon-with-step"
    db.add(_leased_pending(task_id=task_id, execution_state="not_started"))
    db.add(TransferExecutionStep(
        task_id=task_id,
        operation_id="operation-with-step",
        checkpoint_fingerprint="plan",
        ordinal=0,
        phase="transfer",
        kind="move",
        state="prepared",
        attempt_count=0,
        intent_version=1,
        intent_payload={"source": "/mnt/source.mkv"},
        prepared_at="2026-08-27 10:00:00",
        updated_at="2026-08-27 10:00:00",
    ))
    db.session.flush()

    deleted = TransferPending.abandon_unstarted(
        db.session,
        task_id=task_id,
        lease_token=f"lease-{task_id}",
        now_time="2026-08-27 10:01:00.000000",
    )

    assert deleted == 0
    assert db.session.execute(
        select(TransferPending).where(TransferPending.task_id == task_id)
    ).scalar_one_or_none() is not None


def test_abandon_unstarted_rejects_task_with_terminal_history_evidence(db) -> None:
    """已有任务关联历史时不得删除 pending，避免掩盖未闭环结算。"""
    task_id = "abandon-with-history"
    db.add(_leased_pending(task_id=task_id, execution_state="not_started"))
    db.add(TransferHistory(
        transfer_task_id=task_id,
        transfer_settlement_revision=1,
        src="/mnt/abandon-with-history.mkv",
        src_storage="local",
        status=False,
    ))

    deleted = TransferPending.abandon_unstarted(
        db.session,
        task_id=task_id,
        lease_token=f"lease-{task_id}",
        now_time="2026-08-27 10:01:00.000000",
    )

    assert deleted == 0


def test_transactional_repository_rolls_back_failed_write(monkeypatch):
    """适配器写入异常时必须回滚自身 UoW 并传播原异常。"""
    class SessionContext:
        """为回滚断言提供最小 Session 上下文。"""

        def __init__(self):
            """初始化提交与回滚计数。"""
            self.commits = 0
            self.rollbacks = 0

        def __enter__(self):
            """返回当前伪会话。"""
            return self

        def __exit__(self, _exc_type, _exc_value, _traceback):
            """不吞掉被测异常。"""
            return False

        def commit(self):
            """记录提交调用。"""
            self.commits += 1

        def rollback(self):
            """记录回滚调用。"""
            self.rollbacks += 1

    session = SessionContext()
    repository = TransactionalTransferAdmissionRepository(lambda: session)
    monkeypatch.setattr(
        TransferPendingOper,
        "stage_record_enqueue_failure",
        lambda self, **_kwargs: (_ for _ in ()).throw(ValueError("write failed")),
    )

    with pytest.raises(ValueError, match="write failed"):
        repository.record_enqueue_failure(task_id="task", error="failure")

    assert session.rollbacks == 1
    assert session.commits == 0
