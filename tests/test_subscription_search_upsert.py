"""验证活动任务原子合并及非预期约束错误的事务边界。"""

import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.db.adapters.subscriptionsearch import TransactionalSubscriptionSearchRepository
from app.db.models.subscriptionsearch import SubscriptionSearchBatch, SubscriptionSearchTask


@pytest.fixture
def queue(tmp_path):
    """默认使用隔离 SQLite；显式测试 URL 只允许指向可清空的专用数据库。"""
    engine = create_engine(os.environ.get("MOVIEPILOT_TEST_UPSERT_URL", f"sqlite:///{tmp_path / 'queue.db'}"))
    tables = [SubscriptionSearchBatch.__table__, SubscriptionSearchTask.__table__]
    for table in tables:
        table.create(engine, checkfirst=True)
    errors = []
    event.listen(engine, "handle_error", errors.append)
    try:
        yield TransactionalSubscriptionSearchRepository(sessionmaker(bind=engine)), engine, errors
    finally:
        for table in reversed(tables):
            table.drop(engine, checkfirst=True)
        engine.dispose()


def test_concurrent_enqueue_keeps_one_task_without_driver_errors(queue):
    """独立事务同时入队时只有一个创建者，其余请求关联原批次且无驱动异常。"""
    repository, engine, errors = queue
    barrier = Barrier(4)

    def enqueue():
        barrier.wait(timeout=10)
        return repository.enqueue(subscription_ids=(1,), source="fallback", priority=10)

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda _: enqueue(), range(4)))
    assert sum(result.created_count for result in results) == 1
    assert sum(result.coalesced_count for result in results) == 3
    created = next(result for result in results if result.created_count)
    assert all(result.active_batch_ids == (created.batch.batch_id,) for result in results)
    with Session(engine) as session:
        assert len(session.scalars(select(SubscriptionSearchTask)).all()) == 1
    assert errors == []


def test_mixed_enqueue_preserves_batch_identity_and_terminal_reenqueue(queue):
    """同批混合创建和合并保留批次关联，终态释放活动键后可再次创建任务。"""
    repository, engine, errors = queue
    first = repository.enqueue(subscription_ids=(1,), source="fallback", priority=10)
    mixed = repository.enqueue(subscription_ids=(1, 2, 2), source="manual", priority=100)
    assert (mixed.created_count, mixed.coalesced_count) == (1, 1)
    assert mixed.active_batch_ids == (mixed.batch.batch_id, first.batch.batch_id)
    task = repository.claim_next(owner="worker")
    assert task.subscription_id == 1
    assert repository.finish_task(task_id=task.task_id, lease_token=task.lease_token, state="completed")
    again = repository.enqueue(subscription_ids=(1,), source="manual", priority=100)
    assert (again.created_count, again.coalesced_count) == (1, 0)
    with Session(engine) as session:
        rows = session.scalars(select(SubscriptionSearchTask).where(SubscriptionSearchTask.subscription_id == 1)).all()
        assert len(rows) == 2
        assert sum(row.active_key is not None for row in rows) == 1
    assert errors == []


def test_unrelated_constraint_error_rolls_back_entire_batch(queue, monkeypatch):
    """task_id 冲突必须传播，不能误计为活动键合并或留下半个批次。"""
    repository, engine, errors = queue
    repository.enqueue(subscription_ids=(1,), source="fallback", priority=10)
    with Session(engine) as session:
        old_task = session.scalar(select(SubscriptionSearchTask.task_id))

    identifiers = iter(["new-batch", "new-task", old_task])
    monkeypatch.setattr("app.db.oper.subscriptionsearch.uuid4", lambda: SimpleNamespace(hex=next(identifiers)))
    with pytest.raises(IntegrityError):
        repository.enqueue(subscription_ids=(2, 3), source="fallback", priority=10)
    with Session(engine) as session:
        assert len(session.scalars(select(SubscriptionSearchBatch)).all()) == 1
        assert len(session.scalars(select(SubscriptionSearchTask)).all()) == 1
    assert len(errors) == 1
