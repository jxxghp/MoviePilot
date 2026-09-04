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


def test_search_queue_claims_each_subscription_only_after_its_available_at(tmp_path):
    """逐订阅到期时间必须持久化，未到期任务不能占用同步 worker。"""
    repository, _engine = _repository(tmp_path)
    now = datetime.now(timezone.utc)
    ready_at = (now - timedelta(seconds=1)).isoformat(timespec="seconds")
    later_at = (now + timedelta(minutes=5)).isoformat(timespec="seconds")
    repository.enqueue(
        subscription_ids=(20, 21),
        source="fallback",
        priority=10,
        available_at_by_subscription={20: ready_at, 21: later_at},
    )

    first = repository.claim_next(owner="worker-a")

    assert first.subscription_id == 20
    assert first.available_at == ready_at
    assert repository.finish_task(
        task_id=first.task_id,
        lease_token=first.lease_token,
        state="completed",
    ) is True
    assert repository.claim_next(owner="worker-a") is None

    repository.enqueue(
        subscription_ids=(21,),
        source="manual",
        priority=100,
        available_at_by_subscription={21: ready_at},
    )
    accelerated = repository.claim_next(owner="worker-b")
    assert accelerated.subscription_id == 21
    assert accelerated.available_at == ready_at
    assert accelerated.priority == 100


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


def test_search_queue_phase_update_requires_current_lease(tmp_path):
    """过期执行者不得覆盖当前任务的用户可见阶段。"""
    repository, _engine = _repository(tmp_path)
    repository.enqueue(subscription_ids=(30,), source="manual", priority=100)
    task = repository.claim_next(owner="worker-a")

    assert repository.update_task_phase(
        task_id=task.task_id,
        lease_token="stale-token",
        phase="searching",
        current_site_id=7,
    ) is False
    assert repository.update_task_phase(
        task_id=task.task_id,
        lease_token=task.lease_token,
        phase="waiting_site_budget",
        current_site_id=7,
    ) is True

    current = repository.claim_next(owner="worker-b")
    assert current is None
    assert repository.finish_task(
        task_id=task.task_id,
        lease_token=task.lease_token,
        state="completed",
    ) is True


def test_search_queue_defers_site_budget_conflict_until_retry_time(tmp_path):
    """站点预算冲突应释放任务租约并保留同一任务等待后续恢复。"""
    repository, engine = _repository(tmp_path)
    enqueued = repository.enqueue(subscription_ids=(31,), source="fallback", priority=10)
    running = repository.claim_next(owner="worker-a")
    retry_at = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(timespec="seconds")

    assert repository.defer_task(
        task_id=running.task_id,
        lease_token=running.lease_token,
        available_at=retry_at,
    ) is True

    batch = repository.get_batch(enqueued.batch.batch_id)
    assert batch.state == "queued"
    assert batch.finished_count == 0
    assert batch.failed_count == 0
    assert repository.claim_next(owner="worker-b") is None

    with Session(engine) as session:
        task = session.execute(
            select(SubscriptionSearchTask).where(
                SubscriptionSearchTask.task_id == running.task_id
            )
        ).scalar_one()
        assert task.state == "queued"
        assert task.phase == "waiting_site_budget"
        assert task.available_at == retry_at
        assert task.last_error is None
        session.execute(
            update(SubscriptionSearchTask)
            .where(SubscriptionSearchTask.task_id == running.task_id)
            .values(available_at="1970-01-01T00:00:00+00:00")
        )
        session.commit()

    recovered = repository.claim_next(owner="worker-c")
    assert recovered.task_id == running.task_id
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


def test_search_queue_aggregates_skipped_tasks_without_marking_success(tmp_path):
    """跳过任务应单独计数并让批次暴露 skipped 聚合终态。"""
    repository, _engine = _repository(tmp_path)
    enqueued = repository.enqueue(
        subscription_ids=(10, 11),
        source="fallback",
        priority=10,
    )
    first = repository.claim_next(owner="worker-a")
    assert repository.finish_task(
        task_id=first.task_id,
        lease_token=first.lease_token,
        state="skipped",
        error="同一订阅正在由其他通道处理，本轮搜索已跳过",
    ) is True
    second = repository.claim_next(owner="worker-a")
    assert repository.finish_task(
        task_id=second.task_id,
        lease_token=second.lease_token,
        state="completed",
    ) is True

    batch = repository.get_batch(enqueued.batch.batch_id)

    assert batch.state == "skipped"
    assert batch.finished_count == 1
    assert batch.failed_count == 0
    assert batch.cancelled_count == 0
    assert batch.skipped_count == 1
    assert batch.last_error == "同一订阅正在由其他通道处理，本轮搜索已跳过"


def test_search_queue_ages_old_fallback_ahead_of_new_manual_work(tmp_path):
    """手工任务可优先，但等待超过公平窗口的兜底任务不得持续饥饿。"""
    repository, engine = _repository(tmp_path)
    repository.enqueue(subscription_ids=(8,), source="fallback", priority=10)
    aged_at = (datetime.now(timezone.utc) - timedelta(minutes=16)).isoformat(timespec="seconds")
    with Session(engine) as session:
        session.execute(
            update(SubscriptionSearchTask)
            .where(SubscriptionSearchTask.subscription_id == 8)
            .values(created_at=aged_at)
        )
        session.commit()
    repository.enqueue(subscription_ids=(9,), source="manual", priority=100)

    claimed = repository.claim_next(owner="worker-a")

    assert claimed.subscription_id == 8
    assert claimed.source == "fallback"
