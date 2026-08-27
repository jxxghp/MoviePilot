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

from app.db import base as db_base
from app.db.adapters.transfer import TransactionalTransferAdmissionRepository
from app.db.models.transferhistory import TransferHistory
from app.db.models.transferpending import TransferPending
from app.db.oper.transferpending import TransferPendingOper


@pytest.fixture(autouse=True)
def _track(db):
    """把待整理表纳入用例级回收。"""
    db.watermark(TransferPending)


def test_stage_admit_is_idempotent_and_keeps_stable_task_id(db):
    """显式准入重复执行时必须复用首个稳定任务标识。"""
    first = TransferPending.stage_admit(
        db.session,
        task_id="task-first",
        storage="local",
        src_path="/mnt/durable.mkv",
        state="accepted",
        now_time="2026-08-27 10:00:00",
    )
    second = TransferPending.stage_admit(
        db.session,
        task_id="task-second",
        storage="local",
        src_path="/mnt/durable.mkv",
        state="accepted",
        now_time="2026-08-27 11:00:00",
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
    )
    db.add(TransferPending(
        task_id="task-other",
        storage="local",
        src_path="/mnt/other.mkv",
        state="other",
        created_at="2026-08-27 10:00:01",
        updated_at="2026-08-27 10:00:01",
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
    factory = sessionmaker(bind=engine)
    repository = TransactionalTransferAdmissionRepository(factory)

    admitted = repository.admit(
        storage="local",
        src_path="/mnt/repository.mkv",
    )
    repeated = repository.admit(
        storage="local",
        src_path="/mnt/repository.mkv",
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
    assert repository.discard_claimed(
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
