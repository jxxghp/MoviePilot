"""旧待整理 Oper 的精确插件兼容与租约 fencing 测试。"""

import importlib
import inspect
from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.db import base as db_base
from app.db.models.transferexecutionstep import TransferExecutionStep
from app.db.models.transferhistory import TransferHistory
from app.db.models.transferpending import TransferPending
from app.db.oper.transferpending import TransferPendingOper as CanonicalTransferPendingOper
from app.runtime.compat.manifest import MODULE_ALIASES


@pytest.fixture
def legacy_session_factory(tmp_path, monkeypatch):
    """为无 Session 兼容 Oper 提供独占事务，并在提交后保留返回快照。"""
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy-transferpending.db'}")
    TransferPending.__table__.create(engine)
    TransferExecutionStep.__table__.create(engine)
    TransferHistory.__table__.create(engine)
    factory = sessionmaker(bind=engine)

    def run_transaction(operation: Callable[[Session], Any]) -> Any:
        """按生产组合根语义执行一次同步兼容事务。"""
        with factory() as session:
            session.expire_on_commit = False
            try:
                result = operation(session)
                session.commit()
                return result
            except Exception:
                session.rollback()
                raise

    monkeypatch.setattr(db_base, "run_sync_transaction", run_transaction)
    yield factory
    engine.dispose()


def test_legacy_import_targets_private_sdk_facade() -> None:
    """旧模块路径只能解析到私有 SDK 门面，不能回退到 canonical Oper。"""
    alias = MODULE_ALIASES["app.db.transferpending_oper"]
    legacy = importlib.import_module("app.db.transferpending_oper")

    assert alias.target == "app.sdk._legacy.transferpending"
    assert alias.owner == "sdk"
    assert alias.replacement == "app.application.transfer"
    assert legacy is importlib.import_module(alias.target)
    assert legacy.__all__ == ["TransferPendingOper"]
    assert not hasattr(legacy, "TransferPending")
    assert not hasattr(legacy, "TransferExecutionStep")
    assert not hasattr(legacy, "TransferExecutionState")
    assert legacy.TransferPendingOper is not CanonicalTransferPendingOper
    for internal_method in (
            "stage_admit",
            "stage_claim_task",
            "stage_discard_claimed",
            "stage_checkpoint_plan",
    ):
        assert not hasattr(legacy.TransferPendingOper, internal_method)


def test_legacy_oper_preserves_exact_public_method_abi() -> None:
    """兼容门面只公开历史八方法，且位置参数与关键字参数边界保持不变。"""
    legacy = importlib.import_module("app.db.transferpending_oper")
    oper_type = legacy.TransferPendingOper
    public_methods = {
        name
        for name, value in oper_type.__dict__.items()
        if not name.startswith("_") and callable(value)
    }

    assert public_methods == {
        "register",
        "list_by_state",
        "list_by_states",
        "get_by_identity",
        "get_by_task_id",
        "discard",
        "list_all",
        "clear",
    }
    assert str(inspect.signature(oper_type.register)) == (
        "(self, storage: str, src_path: str) -> "
        "app.db.models.transferpending.TransferPending | None"
    )
    assert str(inspect.signature(oper_type.list_by_state)) == (
        "(self, *, state: str, limit: int | None = 5000) -> "
        "List[app.db.models.transferpending.TransferPending]"
    )
    assert str(inspect.signature(oper_type.list_by_states)) == (
        "(self, *, states: tuple[str, ...], limit: int | None = 5000) -> "
        "List[app.db.models.transferpending.TransferPending]"
    )
    assert str(inspect.signature(oper_type.get_by_identity)) == (
        "(self, *, storage: str, src_path: str) -> "
        "app.db.models.transferpending.TransferPending | None"
    )
    assert str(inspect.signature(oper_type.get_by_task_id)) == (
        "(self, *, task_id: str) -> "
        "app.db.models.transferpending.TransferPending | None"
    )
    assert str(inspect.signature(oper_type.discard)) == (
        "(self, storage: str, src_path: str) -> int"
    )
    assert str(inspect.signature(oper_type.list_all)) == (
        "(self, limit: int | None = 5000) -> List[Tuple[str, str]]"
    )
    assert str(inspect.signature(oper_type.clear)) == "(self) -> int"


def test_legacy_no_session_queries_preserve_historical_shapes(
        legacy_session_factory,
) -> None:
    """旧插件无需 Session 即可登记和查询，返回形态与原 ABI 一致。"""
    legacy = importlib.import_module("app.db.transferpending_oper")
    oper = legacy.TransferPendingOper()

    first = oper.register("local", "/downloads/first.mkv")
    repeated = oper.register("local", "/downloads/first.mkv")
    second = oper.register("remote", "/downloads/second.mkv")

    assert first is not None
    assert repeated is not None
    assert second is not None
    assert repeated.task_id == first.task_id
    assert oper.list_all() == [
        ("local", "/downloads/first.mkv"),
        ("remote", "/downloads/second.mkv"),
    ]
    assert [item.task_id for item in oper.list_by_state(state="accepted")] == [
        first.task_id,
        second.task_id,
    ]
    assert [
        item.task_id
        for item in oper.list_by_states(states=("accepted", "planned"), limit=1)
    ] == [first.task_id]
    assert oper.get_by_identity(
        storage="local",
        src_path="/downloads/first.mkv",
    ).task_id == first.task_id
    assert oper.get_by_task_id(task_id=second.task_id).src_path == second.src_path
    assert oper.register("", "/downloads/invalid.mkv") is None
    assert oper.list_by_state(state="") == []
    assert oper.list_by_states(states=()) == []


def test_legacy_register_allows_new_task_for_previously_settled_source(
        legacy_session_factory,
) -> None:
    """旧插件登记入口允许同源文件形成新的合法任务世代。"""
    with legacy_session_factory() as session:
        session.add(TransferHistory(
            transfer_task_id="settled-task",
            transfer_settlement_revision=1,
            src="/downloads/settled.mkv",
            src_storage="local",
            status=True,
        ))
        session.commit()

    legacy = importlib.import_module("app.db.transferpending_oper")
    pending = legacy.TransferPendingOper().register(
        "local",
        "/downloads/settled.mkv",
    )
    assert pending is not None
    assert pending.task_id != "settled-task"
    with legacy_session_factory() as session:
        assert session.execute(select(TransferPending)).scalar_one().task_id == pending.task_id


def test_legacy_mutations_never_delete_claimed_rows(
        legacy_session_factory,
) -> None:
    """旧 discard/clear 只处理新鲜行，有效或过期 token 均受保护。"""
    legacy = importlib.import_module("app.db.transferpending_oper")
    oper = legacy.TransferPendingOper()
    active = oper.register("local", "/downloads/active.mkv")
    expired = oper.register("local", "/downloads/expired.mkv")
    free = oper.register("local", "/downloads/free.mkv")
    assert active is not None
    assert expired is not None
    assert free is not None

    with legacy_session_factory() as session:
        assert TransferPending.claim_task(
            session,
            task_id=active.task_id,
            states=("accepted",),
            owner_id="active-worker",
            lease_token="active-token",
            now_time="2026-08-27 10:00:00.000000",
            lease_expires_at="2026-08-27 10:01:00.000000",
            updated_at="2026-08-27 18:00:00",
        ) == 1
        assert TransferPending.claim_task(
            session,
            task_id=expired.task_id,
            states=("accepted",),
            owner_id="expired-worker",
            lease_token="expired-token",
            now_time="2026-08-27 10:00:00.000000",
            lease_expires_at="2026-08-27 09:59:00.000000",
            updated_at="2026-08-27 18:00:00",
        ) == 1
        session.commit()

    repeated = oper.register("local", "/downloads/active.mkv")
    assert repeated is not None
    assert repeated.task_id == active.task_id
    assert repeated.lease_token == "active-token"
    assert oper.discard("local", "/downloads/active.mkv") == 0
    assert oper.discard("local", "/downloads/expired.mkv") == 0
    assert oper.discard("local", "/downloads/free.mkv") == 1

    removable = oper.register("local", "/downloads/removable.mkv")
    assert removable is not None
    assert oper.clear() == 1
    assert oper.list_all() == [
        ("local", "/downloads/active.mkv"),
        ("local", "/downloads/expired.mkv"),
    ]

    with legacy_session_factory() as session:
        rows = session.execute(
            select(TransferPending).order_by(TransferPending.id.asc())
        ).scalars().all()
        assert [(row.task_id, row.lease_token) for row in rows] == [
            (active.task_id, "active-token"),
            (expired.task_id, "expired-token"),
        ]


def test_legacy_mutations_protect_every_execution_and_terminal_state(
        legacy_session_factory,
) -> None:
    """无租约的运行、等待、结算、失败和人工判定任务仍归状态机所有。"""
    legacy = importlib.import_module("app.db.transferpending_oper")
    oper = legacy.TransferPendingOper()
    protected = {
        state: oper.register("local", f"/downloads/{state}.mkv")
        for state in (
            "running",
            "retry_wait",
            "settling",
            "failed",
            "manual_review",
        )
    }
    expired = oper.register("local", "/downloads/expired-running.mkv")
    safe_discard = oper.register("local", "/downloads/safe-discard.mkv")
    safe_clear = oper.register("local", "/downloads/safe-clear.mkv")
    assert all(protected.values())
    assert expired is not None
    assert safe_discard is not None
    assert safe_clear is not None

    with legacy_session_factory() as session:
        for state, row in protected.items():
            assert row is not None
            session.execute(
                update(TransferPending)
                .where(TransferPending.task_id == row.task_id)
                .values(execution_state=state)
            )
        session.execute(
            update(TransferPending)
            .where(TransferPending.task_id == expired.task_id)
            .values(
                execution_state="running",
                lease_owner="expired-worker",
                lease_token="expired-token",
                lease_expires_at="2026-08-27 09:59:00.000000",
                heartbeat_at="2026-08-27 09:58:00.000000",
                attempt_count=1,
            )
        )
        session.commit()

    assert oper.discard("local", safe_discard.src_path) == 1
    for state, row in protected.items():
        assert row is not None
        assert oper.discard("local", row.src_path) == 0, state
    assert oper.discard("local", expired.src_path) == 0
    assert oper.clear() == 1
    assert set(oper.list_all()) == {
        ("local", row.src_path)
        for row in (*protected.values(), expired)
        if row is not None
    }


def test_legacy_mutations_protect_execution_evidence_even_if_not_started(
        legacy_session_factory,
) -> None:
    """状态字段异常回退时，claim、步骤、重试和结算证据仍阻止旧接口删除。"""
    legacy = importlib.import_module("app.db.transferpending_oper")
    oper = legacy.TransferPendingOper()
    evidence_rows = {
        name: oper.register("local", f"/downloads/evidence-{name}.mkv")
        for name in (
            "claim",
            "checkpoint",
            "retry",
            "settlement",
            "error",
            "step",
            "history",
        )
    }
    safe = oper.register("local", "/downloads/evidence-free.mkv")
    assert all(evidence_rows.values())
    assert safe is not None

    with legacy_session_factory() as session:
        rows = {
            name: row
            for name, row in evidence_rows.items()
            if row is not None
        }
        session.execute(
            update(TransferPending)
            .where(TransferPending.task_id == rows["claim"].task_id)
            .values(attempt_count=1)
        )
        session.execute(
            update(TransferPending)
            .where(TransferPending.task_id == rows["checkpoint"].task_id)
            .values(
                execution_version=1,
                execution_payload={"operation_ids": ["op-checkpoint"]},
                execution_fingerprint="f" * 64,
            )
        )
        session.execute(
            update(TransferPending)
            .where(TransferPending.task_id == rows["retry"].task_id)
            .values(
                retry_generation=1,
                retry_count=1,
                retry_due_at="2026-08-27 12:00:00.000000",
            )
        )
        session.execute(
            update(TransferPending)
            .where(TransferPending.task_id == rows["settlement"].task_id)
            .values(settlement_revision=1, terminal_history_id=42)
        )
        session.execute(
            update(TransferPending)
            .where(TransferPending.task_id == rows["error"].task_id)
            .values(last_error="execution outcome unknown")
        )
        session.add(
            TransferExecutionStep(
                task_id=rows["step"].task_id,
                operation_id="op-step-evidence",
                checkpoint_fingerprint="c" * 64,
                ordinal=0,
                phase="materialize",
                kind="copy",
                state="prepared",
                attempt_count=0,
                intent_version=1,
                intent_payload={"src": rows["step"].src_path},
                prepared_at="2026-08-27 10:00:00.000000",
                updated_at="2026-08-27 10:00:00.000000",
            )
        )
        session.add(
            TransferHistory(
                transfer_task_id=rows["history"].task_id,
                transfer_settlement_revision=1,
                src=rows["history"].src_path,
                src_storage="local",
                status=True,
            )
        )
        session.commit()

    for name, row in evidence_rows.items():
        assert row is not None
        assert oper.discard("local", row.src_path) == 0, name
    assert oper.clear() == 1
    assert set(oper.list_all()) == {
        ("local", row.src_path)
        for row in evidence_rows.values()
        if row is not None
    }


def test_legacy_register_returns_existing_terminal_or_manual_review_task(
        legacy_session_factory,
) -> None:
    """重复登记不得绕过失败或人工判定任务创建平行执行身份。"""
    legacy = importlib.import_module("app.db.transferpending_oper")
    oper = legacy.TransferPendingOper()
    failed = oper.register("local", "/downloads/retry-failed.mkv")
    manual = oper.register("local", "/downloads/retry-manual.mkv")
    assert failed is not None
    assert manual is not None

    with legacy_session_factory() as session:
        session.execute(
            update(TransferPending)
            .where(TransferPending.task_id == failed.task_id)
            .values(
                execution_state="failed",
                settlement_revision=1,
                terminal_history_id=81,
            )
        )
        session.execute(
            update(TransferPending)
            .where(TransferPending.task_id == manual.task_id)
            .values(
                execution_state="manual_review",
                last_error="provider outcome unknown",
            )
        )
        session.commit()

    repeated_failed = oper.register("local", failed.src_path)
    repeated_manual = oper.register("local", manual.src_path)
    assert repeated_failed is not None
    assert repeated_failed.task_id == failed.task_id
    assert repeated_failed.execution_state == "failed"
    assert repeated_failed.terminal_history_id == 81
    assert repeated_manual is not None
    assert repeated_manual.task_id == manual.task_id
    assert repeated_manual.execution_state == "manual_review"
    assert repeated_manual.last_error == "provider outcome unknown"
