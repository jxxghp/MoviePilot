"""订阅搜索持久队列、single-flight、租约和取消测试。"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.db.adapters.subscriptionsearch import TransactionalSubscriptionSearchRepository
from app.db.base import Base
from app.db.models.subscriptionsearch import SubscriptionSearchTask


def _repository(tmp_path):
    """构造使用独立 SQLite 文件的事务型搜索队列。"""
    engine = create_engine(f"sqlite:///{tmp_path / 'search-queue.db'}")
    Base.metadata.create_all(engine)
    return TransactionalSubscriptionSearchRepository(sessionmaker(bind=engine)), engine


def test_search_queue_coalesces_active_subscription_and_raises_priority(tmp_path):
    """重叠入口只保留一个活动任务，手工请求可提高优先级。"""
    repository, _engine = _repository(tmp_path)

    scheduled = repository.enqueue(
        subscription_ids=(1, 2),
        source="fallback",
        priority=10,
    )
    manual = repository.enqueue(
        subscription_ids=(1,),
        source="manual",
        priority=100,
    )

    first = repository.claim_next(owner="worker-a")
    second = repository.claim_next(owner="worker-b")

    assert scheduled.created_count == 2
    assert scheduled.coalesced_count == 0
    assert manual.created_count == 0
    assert manual.coalesced_count == 1
    assert manual.batch.state == "completed"
    assert first.subscription_id == 1
    assert first.priority == 100
    assert second.subscription_id == 2
    assert first.task_id != second.task_id


def test_search_queue_recovers_expired_lease_with_same_task_identity(tmp_path):
    """进程遗留的过期 running 任务应以新 token 恢复且 attempt 单调递增。"""
    repository, engine = _repository(tmp_path)
    repository.enqueue(subscription_ids=(3,), source="fallback", priority=10)
    first = repository.claim_next(owner="worker-a", lease_seconds=900)
    expired_at = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(timespec="seconds")
    with Session(engine) as session:
        session.execute(
            update(SubscriptionSearchTask)
            .where(SubscriptionSearchTask.task_id == first.task_id)
            .values(lease_expires_at=expired_at)
        )
        session.commit()

    recovered = repository.claim_next(owner="worker-b", lease_seconds=900)

    assert recovered.task_id == first.task_id
    assert recovered.subscription_id == first.subscription_id
    assert recovered.lease_token != first.lease_token
    assert recovered.attempt_count == 2


def test_search_queue_cancel_finishes_queued_and_running_tasks(tmp_path):
    """取消立即终止未发请求任务，运行中任务在租约边界收口。"""
    repository, engine = _repository(tmp_path)
    enqueued = repository.enqueue(
        subscription_ids=(4, 5),
        source="fallback",
        priority=10,
    )
    running = repository.claim_next(owner="worker-a")

    assert repository.request_cancel(enqueued.batch.batch_id) is True
    assert repository.is_cancel_requested(running.task_id) is True
    assert repository.release_task(
        task_id=running.task_id,
        lease_token=running.lease_token,
        cancelled=True,
    ) is True

    batch = repository.get_batch(enqueued.batch.batch_id)
    with Session(engine) as session:
        states = list(
            session.execute(
                select(SubscriptionSearchTask.state)
                .where(SubscriptionSearchTask.batch_id == enqueued.batch.batch_id)
                .order_by(SubscriptionSearchTask.position)
            ).scalars()
        )

    assert states == ["cancelled", "cancelled"]
    assert batch.state == "cancelled"
    assert batch.cancelled_count == 2
    assert repository.claim_next(owner="worker-b") is None


def test_search_queue_finishes_batch_with_aggregated_failure(tmp_path):
    """单任务失败不阻止后续任务，但批次最终暴露聚合失败。"""
    repository, _engine = _repository(tmp_path)
    enqueued = repository.enqueue(
        subscription_ids=(6, 7),
        source="fallback",
        priority=10,
    )
    first = repository.claim_next(owner="worker-a")
    assert repository.finish_task(
        task_id=first.task_id,
        lease_token=first.lease_token,
        state="failed",
        error="site timeout",
    ) is True
    second = repository.claim_next(owner="worker-a")
    assert repository.finish_task(
        task_id=second.task_id,
        lease_token=second.lease_token,
        state="completed",
    ) is True

    batch = repository.get_batch(enqueued.batch.batch_id)

    assert batch.state == "failed"
    assert batch.finished_count == 1
    assert batch.failed_count == 1
    assert batch.last_error == "site timeout"
