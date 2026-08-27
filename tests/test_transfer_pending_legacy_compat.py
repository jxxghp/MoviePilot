"""旧待整理 Oper 的精确插件兼容与租约 fencing 测试。"""

import importlib
from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.db import base as db_base
from app.db.models.transferpending import TransferPending
from app.db.oper.transferpending import TransferPendingOper as CanonicalTransferPendingOper
from app.runtime.compat.manifest import MODULE_ALIASES


@pytest.fixture
def legacy_session_factory(tmp_path, monkeypatch):
    """为无 Session 兼容 Oper 提供独占事务，并在提交后保留返回快照。"""
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy-transferpending.db'}")
    TransferPending.__table__.create(engine)
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
    assert legacy.TransferPendingOper is not CanonicalTransferPendingOper
    for internal_method in (
            "stage_admit",
            "stage_claim_task",
            "stage_discard_claimed",
            "stage_checkpoint_plan",
    ):
        assert not hasattr(legacy.TransferPendingOper, internal_method)


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


def test_legacy_mutations_never_delete_claimed_rows(
        legacy_session_factory,
) -> None:
    """旧 discard/clear 只处理未 claim 行，有效或过期 token 均受保护。"""
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
