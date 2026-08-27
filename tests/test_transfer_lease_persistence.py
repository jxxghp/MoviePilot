"""整理恢复租约的原子 claim、续租和陈旧 token 防护测试。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier, Lock
from typing import Any

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from app.application.transfer import (
    TRANSFER_ADMISSION_PLANNED,
    TransferAdmission,
    TransferAdmissionProjectionError,
    TransferLeaseLostError,
    TransferPlanCheckpoint,
    TransferPlanningInput,
    TransferProviderInvocationSnapshot,
    TransferProviderReference,
)
from app.db.adapters.transfer import TransactionalTransferAdmissionRepository
from app.db.models.transferpending import TransferPending
from app.db.oper.transferpending import TransferPendingOper


def _planning_input(path: str) -> TransferPlanningInput:
    """构造与测试源路径绑定的最小规划输入。"""
    return TransferPlanningInput(
        source_fileitem={
            "storage": "local",
            "path": path,
            "type": "file",
            "name": path.rsplit("/", maxsplit=1)[-1],
        },
        meta={"name": "Movie"},
        mediainfo={"title": "Movie"},
    )


def _checkpoint(planning_input: TransferPlanningInput) -> TransferPlanCheckpoint:
    """构造无需文件副作用的合法宿主跳过检查点。"""
    return TransferPlanCheckpoint(
        planning_input=planning_input,
        target_storage="local",
        root_target_path="/library",
        final_target_path="/library",
        resolved_transfer_type="copy",
        items=(),
        skip_reason="测试跳过计划",
    )


def _provider_checkpoint(
        planning_input: TransferPlanningInput,
) -> TransferPlanCheckpoint:
    """构造只冻结 provider ABI、尚未完成宿主规划的检查点。"""
    invocation = TransferProviderInvocationSnapshot(
        fileitem=planning_input.source_fileitem,
        meta=planning_input.meta,
        meta_kind="MetaVideo",
        mediainfo=planning_input.mediainfo,
        mediainfo_kind="MediaInfo",
    )
    return TransferPlanCheckpoint(
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
        legacy_transfer_providers=(
            TransferProviderReference(
                plugin_id="provider-a",
                plugin_name="Provider A",
            ),
        ),
        provider_invocation=invocation,
    )


@pytest.fixture
def repository_factory(tmp_path):
    """创建允许多线程独立 Session 竞争的 SQLite 租约仓储工厂。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'transfer-lease.db'}",
        connect_args={"check_same_thread": False, "timeout": 10},
    )
    TransferPending.__table__.create(engine)
    factory = sessionmaker(bind=engine)
    yield lambda: TransactionalTransferAdmissionRepository(factory)
    engine.dispose()


@pytest.fixture
def lease_clock(monkeypatch):
    """为所有仓储实例提供可推进的固定 UTC 时钟。"""
    clock = {"now": datetime(2026, 8, 27, 10, 0, tzinfo=timezone.utc)}
    monkeypatch.setattr(
        TransactionalTransferAdmissionRepository,
        "_lease_now",
        staticmethod(lambda: clock["now"]),
    )
    return clock


@pytest.fixture
def business_clock(monkeypatch):
    """为业务审计字段提供与 UTC 租约时钟明确分离的宿主本地时间。"""
    clock = {"now": "2026-08-27 18:00:00"}
    monkeypatch.setattr(
        TransactionalTransferAdmissionRepository,
        "_now",
        staticmethod(lambda: clock["now"]),
    )
    return clock


def _admit(
        repository: TransactionalTransferAdmissionRepository,
        path: str,
) -> TransferAdmission:
    """登记一个带完整版本化输入的测试任务。"""
    return repository.admit(
        storage="local",
        src_path=path,
        planning_input=_planning_input(path),
    )


def _pending_snapshot(
        repository: TransactionalTransferAdmissionRepository,
        task_id: str,
) -> dict[str, Any]:
    """在 Session 关闭前冻结测试需要检查的持久登记字段。"""
    with repository._session_factory() as session:  # noqa: SLF001
        pending = session.execute(
            select(TransferPending).where(TransferPending.task_id == task_id)
        ).scalar_one()
        return {
            "lease_owner": pending.lease_owner,
            "lease_token": pending.lease_token,
            "lease_expires_at": pending.lease_expires_at,
            "heartbeat_at": pending.heartbeat_at,
            "last_error": pending.last_error,
            "attempt_count": pending.attempt_count,
            "updated_at": pending.updated_at,
            "planned_at": pending.planned_at,
        }


def test_claim_heartbeat_expired_takeover_and_stale_token_guards(
        repository_factory,
        lease_clock,
        business_clock,
) -> None:
    """新 token 才增加 attempt，过期 token 不能续租、释放或删除接管者。"""
    repository = repository_factory()
    admitted = _admit(repository, "/downloads/movie.mkv")
    assert admitted.created_at == "2026-08-27 18:00:00"
    business_clock["now"] = "2026-08-27 18:01:00"

    first = repository.claim_task(
        task_id=admitted.task_id,
        owner_id="worker-a",
        lease_seconds=60,
    )
    assert first is not None
    assert first.lease_owner == "worker-a"
    assert first.lease_token
    assert first.attempt_count == 1
    assert first.updated_at == "2026-08-27 18:01:00"

    assert repository.claim_task(
        task_id=admitted.task_id,
        owner_id="worker-a",
        lease_seconds=60,
    ) is None
    assert repository.claim_task(
        task_id=admitted.task_id,
        owner_id="worker-b",
        lease_seconds=60,
    ) is None

    lease_clock["now"] += timedelta(seconds=30)
    business_clock["now"] = "2026-08-27 18:02:00"
    renewed = repository.heartbeat(
        task_id=admitted.task_id,
        lease_token=first.lease_token,
        lease_seconds=60,
    )
    assert renewed is not None
    assert renewed.attempt_count == 1
    assert renewed.heartbeat_at == "2026-08-27 10:00:30.000000"
    assert renewed.updated_at == first.updated_at

    lease_clock["now"] += timedelta(seconds=61)
    business_clock["now"] = "2026-08-27 18:03:00"
    assert repository.release_claim(
        task_id=admitted.task_id,
        lease_token=first.lease_token,
        error="expired worker",
    ) is False
    assert repository.discard_claimed(
        task_id=admitted.task_id,
        lease_token=first.lease_token,
    ) == 0
    takeover = repository.claim_task(
        task_id=admitted.task_id,
        owner_id="worker-b",
        lease_seconds=60,
    )
    assert takeover is not None
    assert takeover.lease_token != first.lease_token
    assert takeover.attempt_count == 2
    assert takeover.updated_at == "2026-08-27 18:03:00"
    assert repository.heartbeat(
        task_id=admitted.task_id,
        lease_token=first.lease_token,
        lease_seconds=60,
    ) is None
    business_clock["now"] = "2026-08-27 18:04:00"
    assert repository.release_claim(
        task_id=admitted.task_id,
        lease_token=first.lease_token,
        error="stale worker",
    ) is False
    assert repository.discard_claimed(
        task_id=admitted.task_id,
        lease_token=first.lease_token,
    ) == 0

    assert repository.release_claim(
        task_id=admitted.task_id,
        lease_token=takeover.lease_token,
        error="retry later",
    ) is True
    released = _pending_snapshot(repository, admitted.task_id)
    assert released["lease_owner"] is None
    assert released["lease_token"] is None
    assert released["lease_expires_at"] is None
    assert released["heartbeat_at"] is None
    assert released["last_error"] == "retry later"
    assert released["attempt_count"] == 2
    assert released["updated_at"] == "2026-08-27 18:04:00"

    third = repository.claim_task(
        task_id=admitted.task_id,
        owner_id="worker-c",
        lease_seconds=60,
    )
    assert third is not None
    assert third.attempt_count == 3
    assert repository.discard_claimed(
        task_id=admitted.task_id,
        lease_token=third.lease_token,
    ) == 1
    with repository._session_factory() as session:  # noqa: SLF001
        assert session.execute(
            select(TransferPending).where(
                TransferPending.task_id == admitted.task_id
            )
        ).scalar_one_or_none() is None


def test_claim_recoverable_respects_order_limit_and_active_lease(
        repository_factory,
        lease_clock,
) -> None:
    """批量恢复跳过有效租约，并按登记顺序逐条 CAS 到请求上限。"""
    repository = repository_factory()
    first = _admit(repository, "/downloads/a.mkv")
    second = _admit(repository, "/downloads/b.mkv")
    third = _admit(repository, "/downloads/c.mkv")
    active = repository.claim_task(
        task_id=first.task_id,
        owner_id="active-worker",
        lease_seconds=60,
    )
    assert active is not None

    claimed = repository.claim_recoverable(
        owner_id="recovery-worker",
        limit=2,
        lease_seconds=60,
    )

    assert [item.task_id for item in claimed] == [second.task_id, third.task_id]
    assert all(item.lease_owner == "recovery-worker" for item in claimed)
    assert all(item.attempt_count == 1 for item in claimed)
    assert repository.claim_recoverable(
        owner_id="other-worker",
        limit=10,
        lease_seconds=60,
    ) == []

    lease_clock["now"] += timedelta(seconds=61)
    reclaimed = repository.claim_recoverable(
        owner_id="takeover-worker",
        limit=2,
        lease_seconds=60,
    )
    assert [item.task_id for item in reclaimed] == [first.task_id, second.task_id]
    assert reclaimed[0].attempt_count == 2
    assert reclaimed[1].attempt_count == 2


def test_claim_recoverable_skips_corrupt_projection_and_claims_later_tasks(
        repository_factory,
        lease_clock,
        business_clock,
        monkeypatch,
) -> None:
    """毒行应留下单次诊断但不持有租约或饿死后续健康任务。"""
    repository = repository_factory()
    messages: list[str] = []
    monkeypatch.setattr(
        "app.db.adapters.transfer._diagnostic_logger.error",
        messages.append,
    )
    corrupt = _admit(repository, "/downloads/a-corrupt.mkv")
    healthy = [
        _admit(repository, "/downloads/b-healthy.mkv"),
        _admit(repository, "/downloads/c-healthy.mkv"),
    ]
    with repository._session_factory() as session:  # noqa: SLF001
        pending = session.execute(
            select(TransferPending).where(
                TransferPending.task_id == corrupt.task_id
            )
        ).scalar_one()
        pending.input_fingerprint = "corrupt"
        session.commit()

    claimed = repository.claim_recoverable(
        owner_id="recovery-worker",
        limit=2,
        lease_seconds=60,
    )

    assert [item.task_id for item in claimed] == [item.task_id for item in healthy]
    corrupt_snapshot = _pending_snapshot(repository, corrupt.task_id)
    assert corrupt_snapshot["lease_token"] is None
    assert corrupt_snapshot["attempt_count"] == 0
    assert corrupt_snapshot["last_error"].startswith("恢复投影失败:")
    assert corrupt_snapshot["updated_at"] == "2026-08-27 18:00:00"
    assert len(messages) == 1

    business_clock["now"] = "2026-08-27 18:01:00"
    assert repository.claim_recoverable(
        owner_id="second-recovery-worker",
        limit=1,
        lease_seconds=60,
    ) == []
    assert _pending_snapshot(repository, corrupt.task_id) == corrupt_snapshot
    assert len(messages) == 1

    with repository._session_factory() as session:  # noqa: SLF001
        pending = session.execute(
            select(TransferPending).where(
                TransferPending.task_id == corrupt.task_id
            )
        ).scalar_one()
        pending.input_fingerprint = _planning_input(
            "/downloads/a-corrupt.mkv"
        ).fingerprint
        session.commit()

    repaired = repository.claim_recoverable(
        owner_id="repaired-worker",
        limit=1,
        lease_seconds=60,
    )
    assert [item.task_id for item in repaired] == [corrupt.task_id]


def test_projection_diagnostic_changes_are_recorded_once_each(
        repository_factory,
        business_clock,
        monkeypatch,
) -> None:
    """相同投影错误不重复写库，错误类型变化时才更新诊断并再次告警。"""
    repository = repository_factory()
    messages: list[str] = []
    monkeypatch.setattr(
        "app.db.adapters.transfer._diagnostic_logger.error",
        messages.append,
    )
    admitted = _admit(repository, "/downloads/changing-corrupt.mkv")
    with repository._session_factory() as session:  # noqa: SLF001
        pending = session.execute(
            select(TransferPending).where(
                TransferPending.task_id == admitted.task_id
            )
        ).scalar_one()
        pending.input_fingerprint = "corrupt"
        session.commit()

    assert repository.claim_recoverable(
        owner_id="recovery-a",
        limit=1,
        lease_seconds=60,
    ) == []
    first = _pending_snapshot(repository, admitted.task_id)
    assert len(messages) == 1

    business_clock["now"] = "2026-08-27 18:01:00"
    assert repository.claim_recoverable(
        owner_id="recovery-b",
        limit=1,
        lease_seconds=60,
    ) == []
    assert _pending_snapshot(repository, admitted.task_id) == first
    assert len(messages) == 1

    with repository._session_factory() as session:  # noqa: SLF001
        pending = session.execute(
            select(TransferPending).where(
                TransferPending.task_id == admitted.task_id
            )
        ).scalar_one()
        pending.input_fingerprint = _planning_input(
            "/downloads/changing-corrupt.mkv"
        ).fingerprint
        pending.input_version = 999
        session.commit()

    business_clock["now"] = "2026-08-27 18:02:00"
    assert repository.claim_recoverable(
        owner_id="recovery-c",
        limit=1,
        lease_seconds=60,
    ) == []
    changed = _pending_snapshot(repository, admitted.task_id)
    assert changed["last_error"] != first["last_error"]
    assert changed["updated_at"] == "2026-08-27 18:02:00"
    assert len(messages) == 2


def test_projection_diagnostic_cas_is_concurrency_safe(
        repository_factory,
        monkeypatch,
) -> None:
    """并发恢复观察到同一损坏时只允许一个诊断写入者和一条运行日志。"""
    repository = repository_factory()
    admitted = _admit(repository, "/downloads/concurrent-corrupt.mkv")
    messages: list[str] = []
    message_lock = Lock()

    def capture(message: str) -> None:
        """并发安全收集错误日志。"""
        with message_lock:
            messages.append(message)

    monkeypatch.setattr(
        "app.db.adapters.transfer._diagnostic_logger.error",
        capture,
    )
    barrier = Barrier(2)
    projection_error = TransferAdmissionProjectionError("same corruption")

    def record(_: int) -> bool:
        """让两个独立 Session 同时竞争同一诊断 CAS。"""
        barrier.wait(timeout=5)
        return repository_factory()._record_projection_failure(  # noqa: SLF001
            task_id=admitted.task_id,
            error=projection_error,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(record, range(2)))

    assert sorted(results) == [False, True]
    assert len(messages) == 1
    snapshot = _pending_snapshot(repository, admitted.task_id)
    assert snapshot["last_error"] == "恢复投影失败: same corruption"


def test_projection_diagnostic_does_not_overwrite_active_lease(
        repository_factory,
        monkeypatch,
) -> None:
    """诊断 CAS 不得覆盖已经由健康 worker 取得有效租约的任务。"""
    repository = repository_factory()
    admitted = _admit(repository, "/downloads/active-lease.mkv")
    claimed = repository.claim_task(
        task_id=admitted.task_id,
        owner_id="active-worker",
        lease_seconds=60,
    )
    assert claimed is not None
    before = _pending_snapshot(repository, admitted.task_id)
    messages: list[str] = []
    monkeypatch.setattr(
        "app.db.adapters.transfer._diagnostic_logger.error",
        messages.append,
    )

    recorded = repository._record_projection_failure(  # noqa: SLF001
        task_id=admitted.task_id,
        error=TransferAdmissionProjectionError("stale observation"),
    )

    assert recorded is False
    assert _pending_snapshot(repository, admitted.task_id) == before
    assert messages == []


def test_projection_diagnostic_database_failure_propagates(
        repository_factory,
        monkeypatch,
) -> None:
    """诊断留痕的数据库基础设施异常必须向上游传播而非静默跳过。"""
    repository = repository_factory()
    admitted = _admit(repository, "/downloads/db-error-corrupt.mkv")
    with repository._session_factory() as session:  # noqa: SLF001
        pending = session.execute(
            select(TransferPending).where(
                TransferPending.task_id == admitted.task_id
            )
        ).scalar_one()
        pending.input_fingerprint = "corrupt"
        session.commit()

    def fail_diagnostic(*_args, **_kwargs):
        """模拟诊断短事务的底层数据库写入失败。"""
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(
        TransferPendingOper,
        "stage_record_projection_failure",
        fail_diagnostic,
    )

    with pytest.raises(RuntimeError, match="database unavailable"):
        repository.claim_recoverable(
            owner_id="recovery-worker",
            limit=1,
            lease_seconds=60,
        )


def test_claim_task_wraps_persisted_json_decode_failure(
        repository_factory,
) -> None:
    """持久 JSON 解码错误应归类为投影损坏，而不是数据库基础设施故障。"""
    repository = repository_factory()
    admitted = _admit(repository, "/downloads/invalid-json.mkv")
    with repository._session_factory() as session:  # noqa: SLF001
        session.execute(
            text(
                "UPDATE transferpending SET planning_input = 'not-json' "
                "WHERE task_id = :task_id"
            ),
            {"task_id": admitted.task_id},
        )
        session.commit()

    with pytest.raises(TransferAdmissionProjectionError, match="JSON"):
        repository.claim_task(
            task_id=admitted.task_id,
            owner_id="recovery-worker",
            lease_seconds=60,
        )
    with repository._session_factory() as session:  # noqa: SLF001
        snapshot = session.execute(
            text(
                "SELECT lease_token, attempt_count FROM transferpending "
                "WHERE task_id = :task_id"
            ),
            {"task_id": admitted.task_id},
        ).mappings().one()
    assert snapshot["lease_token"] is None
    assert snapshot["attempt_count"] == 0


def test_concurrent_recovery_callers_scan_past_lost_candidates(
        repository_factory,
        lease_clock,
        monkeypatch,
) -> None:
    """并发 caller 竞争同一首批后应继续向后扫描并各自填满限额。"""
    setup_repository = repository_factory()
    admitted = [
        _admit(setup_repository, f"/downloads/concurrent-{index}.mkv")
        for index in range(4)
    ]
    barrier = Barrier(2)
    barrier_lock = Lock()
    initial_scans = 0
    original = TransferPending.list_claimable_candidates.__func__

    def synchronized_candidates(cls, db, **kwargs):
        """强制两个 caller 在取得相同首批候选后再进入逐任务 CAS。"""
        nonlocal initial_scans
        candidates = original(cls, db, **kwargs)
        if kwargs.get("after_cursor") is None:
            with barrier_lock:
                initial_scans += 1
            barrier.wait(timeout=5)
        return candidates

    monkeypatch.setattr(
        TransferPending,
        "list_claimable_candidates",
        classmethod(synchronized_candidates),
    )

    def recover(owner_id: str) -> list[TransferAdmission]:
        """使用独立仓储与 Session 执行一次有界恢复扫描。"""
        return repository_factory().claim_recoverable(
            owner_id=owner_id,
            limit=2,
            lease_seconds=60,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(recover, ("worker-a", "worker-b")))

    assert initial_scans == 2
    assert [len(result) for result in results] == [2, 2]
    claimed_task_ids = [item.task_id for result in results for item in result]
    assert len(set(claimed_task_ids)) == 4
    assert set(claimed_task_ids) == {item.task_id for item in admitted}


def test_unclaimed_enqueue_failure_cannot_overwrite_claimed_task(
        repository_factory,
        business_clock,
) -> None:
    """task-id-only 入队失败入口不得改写已经由 worker claim 的登记。"""
    repository = repository_factory()
    admitted = _admit(repository, "/downloads/claimed.mkv")
    business_clock["now"] = "2026-08-27 18:01:00"
    claimed = repository.claim_task(
        task_id=admitted.task_id,
        owner_id="worker-a",
        lease_seconds=60,
    )
    assert claimed is not None
    before = _pending_snapshot(repository, admitted.task_id)

    business_clock["now"] = "2026-08-27 18:02:00"
    repository.record_enqueue_failure(
        task_id=admitted.task_id,
        error="stale queue failure",
    )

    after = _pending_snapshot(repository, admitted.task_id)
    assert after == before


def test_planning_writes_require_current_unexpired_lease(
        repository_factory,
        lease_clock,
) -> None:
    """checkpoint 和规划错误均不得由已过期或已被接管的 worker 写入。"""
    repository = repository_factory()
    path = "/downloads/planning.mkv"
    planning_input = _planning_input(path)
    admitted = repository.admit(
        storage="local",
        src_path=path,
        planning_input=planning_input,
    )
    first = repository.claim_task(
        task_id=admitted.task_id,
        owner_id="worker-a",
        lease_seconds=30,
    )
    assert first is not None

    lease_clock["now"] += timedelta(seconds=31)
    takeover = repository.claim_task(
        task_id=admitted.task_id,
        owner_id="worker-b",
        lease_seconds=60,
    )
    assert takeover is not None

    with pytest.raises(TransferLeaseLostError, match="租约"):
        repository.record_planning_failure(
            task_id=admitted.task_id,
            lease_token=first.lease_token,
            error="stale planning failure",
        )
    with pytest.raises(TransferLeaseLostError, match="租约"):
        repository.checkpoint_plan(
            task_id=admitted.task_id,
            lease_token=first.lease_token,
            input_fingerprint=planning_input.fingerprint,
            checkpoint=_checkpoint(planning_input),
        )

    repository.record_planning_failure(
        task_id=admitted.task_id,
        lease_token=takeover.lease_token,
        error="retryable planning failure",
    )
    planned = repository.checkpoint_plan(
        task_id=admitted.task_id,
        lease_token=takeover.lease_token,
        input_fingerprint=planning_input.fingerprint,
        checkpoint=_checkpoint(planning_input),
    )
    repeated = repository.checkpoint_plan(
        task_id=admitted.task_id,
        lease_token=takeover.lease_token,
        input_fingerprint=planning_input.fingerprint,
        checkpoint=_checkpoint(planning_input),
    )

    assert planned.state == TRANSFER_ADMISSION_PLANNED
    assert planned.last_error is None
    assert repeated == planned
    assert planned.attempt_count == 2


def test_provider_checkpoint_sets_planned_time_only_after_host_plan(
        repository_factory,
        lease_clock,
        business_clock,
) -> None:
    """provider 快照不是规划完成，planned_at 只记录首次宿主完整计划。"""
    repository = repository_factory()
    path = "/downloads/provider-plan.mkv"
    planning_input = _planning_input(path)
    admitted = repository.admit(
        storage="local",
        src_path=path,
        planning_input=planning_input,
    )
    business_clock["now"] = "2026-08-27 18:01:00"
    claimed = repository.claim_task(
        task_id=admitted.task_id,
        owner_id="worker-a",
        lease_seconds=60,
    )
    assert claimed is not None

    business_clock["now"] = "2026-08-27 18:02:00"
    repository.checkpoint_plan(
        task_id=admitted.task_id,
        lease_token=claimed.lease_token,
        input_fingerprint=planning_input.fingerprint,
        checkpoint=_provider_checkpoint(planning_input),
    )
    provider_snapshot = _pending_snapshot(repository, admitted.task_id)
    assert provider_snapshot["planned_at"] is None
    assert provider_snapshot["updated_at"] == "2026-08-27 18:02:00"

    business_clock["now"] = "2026-08-27 18:03:00"
    checkpoint = _checkpoint(planning_input)
    repository.checkpoint_plan(
        task_id=admitted.task_id,
        lease_token=claimed.lease_token,
        input_fingerprint=planning_input.fingerprint,
        checkpoint=checkpoint,
    )
    planned_snapshot = _pending_snapshot(repository, admitted.task_id)
    assert planned_snapshot["planned_at"] == "2026-08-27 18:03:00"

    business_clock["now"] = "2026-08-27 18:04:00"
    repository.checkpoint_plan(
        task_id=admitted.task_id,
        lease_token=claimed.lease_token,
        input_fingerprint=planning_input.fingerprint,
        checkpoint=checkpoint,
    )
    assert _pending_snapshot(repository, admitted.task_id) == planned_snapshot


def test_concurrent_claim_uses_rowcount_as_single_winner(
        repository_factory,
        lease_clock,
) -> None:
    """并发 worker 即使读取同一任务，也只能有一个 CAS 更新获胜。"""
    setup_repository = repository_factory()
    admitted = _admit(setup_repository, "/downloads/concurrent.mkv")
    barrier = Barrier(2)

    def claim(owner_id: str):
        """等待竞争者就绪后使用独立 Session claim 同一任务。"""
        repository = repository_factory()
        barrier.wait()
        return repository.claim_task(
            task_id=admitted.task_id,
            owner_id=owner_id,
            lease_seconds=60,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(claim, ("worker-a", "worker-b")))

    winners = [result for result in results if result is not None]
    assert len(winners) == 1
    assert winners[0].attempt_count == 1
