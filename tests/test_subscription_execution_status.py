"""订阅执行状态合并、批次权限和操作能力测试。"""

import asyncio
from dataclasses import replace

from app.application.subscription.execution import SearchBatchSnapshot, SearchTaskSnapshot
from app.application.subscription.status import SubscriptionExecutionStatusService


def _task(
    subscription_id: int,
    *,
    state: str = "running",
    phase: str = "searching",
    updated_at: str = "2026-09-01T01:00:00+00:00",
    batch_id: str = "batch-1",
    source: str = "manual",
    available_at: str | None = None,
) -> SearchTaskSnapshot:
    """构造最小搜索任务快照。"""
    return SearchTaskSnapshot(
        task_id=f"task-{subscription_id}",
        batch_id=batch_id,
        subscription_id=subscription_id,
        source=source,
        priority=100,
        position=subscription_id,
        state=state,
        phase=phase,
        attempt_count=1,
        cancel_requested=False,
        lease_token="lease" if state == "running" else None,
        created_at="2026-09-01T00:00:00+00:00",
        updated_at=updated_at,
        available_at=available_at,
        current_site_id=9 if phase == "waiting_site_budget" else None,
        last_error=(
            " provider\n timeout "
            if state == "failed"
            else "站点暂时忙，系统会自动继续搜索"
            if state == "queued" and phase == "waiting_site_budget"
            else None
        ),
    )


class _Repository:
    """保存测试快照的异步状态仓储。"""

    def __init__(self) -> None:
        """初始化可由测试覆盖的快照集合。"""
        self.tasks: dict[int, SearchTaskSnapshot] = {}
        self.batch = SearchBatchSnapshot(
            batch_id="batch-1",
            source="manual",
            state="running",
            priority=100,
            total_count=2,
            finished_count=0,
            failed_count=0,
            cancelled_count=0,
            cancel_requested=False,
            created_at="2026-09-01T00:00:00+00:00",
            updated_at="2026-09-01T01:00:00+00:00",
        )

    async def latest_search_tasks(self, subscription_ids):
        """返回请求范围内搜索任务。"""
        return {key: value for key, value in self.tasks.items() if key in subscription_ids}

    async def list_batches(self, *, limit):
        """返回一个测试批次。"""
        return [self.batch][:limit]

    async def get_batch(self, batch_id):
        """按 ID 返回测试批次。"""
        return self.batch if batch_id == self.batch.batch_id else None

    async def list_batch_tasks(self, batch_id):
        """返回属于测试批次的任务。"""
        return [task for task in self.tasks.values() if task.batch_id == batch_id]


def test_execution_status_exposes_site_wait_and_cancel_capability():
    """站点预算等待必须保留当前站点和取消能力。"""
    repository = _Repository()
    repository.tasks[1] = _task(1, phase="waiting_site_budget")

    statuses = asyncio.run(SubscriptionExecutionStatusService(repository).for_subscriptions((1,)))

    assert statuses[1].state == "waiting_site_budget"
    assert statuses[1].current_site_id == 9
    assert statuses[1].can_cancel is True


def test_execution_status_exposes_queued_site_wait_without_error():
    """重新入队的站点繁忙应显示等待状态、说明和继续时间。"""
    repository = _Repository()
    retry_at = "2026-09-01T01:00:10+00:00"
    repository.tasks[2] = _task(
        2,
        state="queued",
        phase="waiting_site_budget",
        available_at=retry_at,
    )

    statuses = asyncio.run(SubscriptionExecutionStatusService(repository).for_subscriptions((2,)))

    assert statuses[2].state == "waiting_site_budget"
    assert statuses[2].phase == "waiting_site_budget"
    assert statuses[2].error == "站点暂时忙，系统会自动继续搜索"
    assert statuses[2].next_run_at == retry_at
    assert statuses[2].can_cancel is True


def test_execution_status_exposes_scheduled_new_search_without_failure():
    """新订阅编辑等待期应显示为已安排，而不是跳过或失败。"""
    repository = _Repository()
    retry_at = "2026-09-01T01:01:00+00:00"
    repository.tasks[4] = _task(
        4,
        state="queued",
        phase="scheduled",
        source="new",
        available_at=retry_at,
    )

    statuses = asyncio.run(SubscriptionExecutionStatusService(repository).for_subscriptions((4,)))

    assert statuses[4].state == "scheduled"
    assert statuses[4].next_run_at == retry_at
    assert statuses[4].error is None


def test_failed_search_exposes_safe_error():
    """搜索失败文本必须压平且不暴露内部错误细节。"""
    repository = _Repository()
    repository.tasks[3] = _task(3, state="failed", phase="failed")

    statuses = asyncio.run(SubscriptionExecutionStatusService(repository).for_subscriptions((3,)))

    assert statuses[3].state == "failed"
    assert statuses[3].error == "订阅操作失败，请刷新后重试"


def test_batch_requires_complete_subscription_access():
    """普通用户不得读取混合其他 owner 订阅的批次聚合。"""
    repository = _Repository()
    repository.tasks = {1: _task(1), 2: _task(2)}
    service = SubscriptionExecutionStatusService(repository)

    hidden = asyncio.run(service.get_batch("batch-1", accessible_subscription_ids={1}))
    visible = asyncio.run(service.get_batch("batch-1", accessible_subscription_ids={1, 2}))

    assert hidden is None
    assert visible is not None
    assert visible.current_subscription_id == 1
    assert visible.processed_count == 0
    assert visible.can_cancel is True


def test_batch_projection_exposes_skipped_count_as_processed_without_success():
    """批次跳过应计入处理总数，同时保留独立的完成计数。"""
    repository = _Repository()
    repository.batch = replace(
        repository.batch,
        state="skipped",
        total_count=2,
        finished_count=1,
        skipped_count=1,
    )
    repository.tasks = {
        1: _task(1, state="completed", phase="completed"),
        2: _task(2, state="skipped", phase="skipped"),
    }

    visible = asyncio.run(
        SubscriptionExecutionStatusService(repository).get_batch(
            "batch-1",
            accessible_subscription_ids={1, 2},
        )
    )

    assert visible is not None
    assert visible.state == "skipped"
    assert visible.finished_count == 1
    assert visible.skipped_count == 1
    assert visible.processed_count == 2


def test_request_cancel_uses_injected_execution_boundary():
    """取消必须通过组合根注入的异步执行边界并返回真实结果。"""
    repository = _Repository()
    requested: list[str] = []

    async def request_cancel(batch_id: str) -> bool:
        """记录测试请求并模拟队列接受取消。"""
        requested.append(batch_id)
        return True

    service = SubscriptionExecutionStatusService(
        repository,
        request_cancel=request_cancel,
    )

    assert asyncio.run(service.request_cancel("batch-1")) is True
    assert requested == ["batch-1"]
