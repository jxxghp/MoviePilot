import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.api.dependencies import subscription as subscription_dependencies
from app.application.subscription.delete import SubscribeDeletionCandidate
from app.application.subscription.search import (
    SearchSubscriptionsCommand,
    SubscribeSearchActor,
    SubscriptionSearchSubmission,
)
from app.runtime.tasks import TaskRegistry


class _Repository:
    """提供手工订阅搜索测试需要的归属和列表数据。"""

    def __init__(self, candidate=None, subscribe_ids=None):
        """保存预设单条候选和批量编号。"""
        self.candidate = candidate
        self.subscribe_ids = subscribe_ids or []
        self.list_calls = []

    async def get_candidate(self, _subscribe_id):
        """返回预设订阅候选。"""
        return self.candidate

    async def list_search_ids(self, username, state):
        """记录访问范围并返回预设编号。"""
        self.list_calls.append((username, state))
        return self.subscribe_ids


def _candidate(username):
    """构造只包含归属信息的订阅候选。"""
    return SubscribeDeletionCandidate(
        subscribe_id=7,
        username=username,
        event_payload={},
    )


def _submitter(calls):
    """构造记录目标并返回稳定安排结果的异步提交器。"""
    async def submit(subscribe_ids, single):
        """记录一次轻量入队请求。"""
        calls.append((subscribe_ids, single))
        return SubscriptionSearchSubmission(
            batch_ids=("batch-1",),
            target_count=len(subscribe_ids),
            queued_count=len(subscribe_ids),
            ongoing_count=0,
            single=single,
        )

    return submit


@pytest.mark.asyncio
async def test_superuser_search_all_queues_every_active_subscription():
    """管理员搜索全部订阅时读取全局活动订阅并一次提交。"""
    submitted = []
    repository = _Repository(subscribe_ids=[2, 5])
    command = SearchSubscriptionsCommand(
        repository=repository,
        submit_search=_submitter(submitted),
    )

    result = await command.execute(
        SubscribeSearchActor(username="admin", is_superuser=True)
    )

    assert result is not None
    assert result.target_count == 2
    assert repository.list_calls == [(None, "R")]
    assert submitted == [((2, 5), False)]


@pytest.mark.asyncio
async def test_regular_user_search_all_schedules_only_owned_subscriptions():
    """普通用户搜索全部时把归属订阅合并为一次后台批次。"""
    submitted = []
    repository = _Repository(subscribe_ids=[2, 5])
    command = SearchSubscriptionsCommand(
        repository=repository,
        submit_search=_submitter(submitted),
    )

    result = await command.execute(
        SubscribeSearchActor(username="alice", is_superuser=False)
    )

    assert result is not None
    assert repository.list_calls == [("alice", "R")]
    assert submitted == [((2, 5), False)]


@pytest.mark.asyncio
async def test_regular_user_search_all_with_no_targets_does_not_schedule():
    """普通用户没有可搜索订阅时不创建空后台任务。"""
    submitted = []
    command = SearchSubscriptionsCommand(
        repository=_Repository(),
        submit_search=_submitter(submitted),
    )

    result = await command.execute(
        SubscribeSearchActor(username="alice", is_superuser=False)
    )

    assert result is not None
    assert result.target_count == 0
    assert result.batch_ids == ()
    assert submitted == []


@pytest.mark.asyncio
async def test_targeted_search_rejects_missing_or_other_users_subscription():
    """单条搜索不得泄漏订阅是否属于其他普通用户。"""
    submitted = []
    command = SearchSubscriptionsCommand(
        repository=_Repository(candidate=_candidate("bob")),
        submit_search=_submitter(submitted),
    )

    assert await command.execute(
        SubscribeSearchActor(username="alice", is_superuser=False),
        subscribe_id=7,
    ) is None
    assert submitted == []


@pytest.mark.asyncio
async def test_targeted_search_schedules_accessible_subscription():
    """归属用户搜索单条订阅时提交历史兼容参数。"""
    submitted = []
    command = SearchSubscriptionsCommand(
        repository=_Repository(candidate=_candidate("alice")),
        submit_search=_submitter(submitted),
    )

    result = await command.execute(
        SubscribeSearchActor(username="alice", is_superuser=False),
        subscribe_id=7,
    )

    assert result is not None
    assert result.single is True
    assert submitted == [((7,), True)]


def test_submitted_search_wakes_short_cycle_queue(monkeypatch):
    """手工搜索入队后立即唤醒短周期恢复任务。"""
    calls = []
    monkeypatch.setattr(
        subscription_dependencies,
        "start_scheduler_job",
        lambda job_id, **kwargs: calls.append((job_id, kwargs)),
    )

    subscription_dependencies._resume_submitted_subscription_search((2, 5))

    assert calls == [
        (
            "subscribe_search_queue",
            {"limit": 2, "manual_sids": (2, 5)},
        ),
    ]


@pytest.mark.asyncio
async def test_search_dependency_persists_before_waking_background_worker():
    """请求适配器先等待持久入队完成，再登记后台恢复任务。"""
    registry = TaskRegistry()
    calls = []

    def create_sync(function, *args, owner, **kwargs):
        """同步执行测试函数，并返回已经完成的 Future。"""
        calls.append((function, args, owner, kwargs))
        future = asyncio.get_running_loop().create_future()
        result = function(*args, **kwargs) if owner.endswith("enqueue") else None
        future.set_result(result)
        return future

    registry.create_sync = Mock(side_effect=create_sync)
    repository = _Repository(subscribe_ids=[2, 5])
    search_repository = SimpleNamespace(
        enqueue=Mock(
            return_value=SimpleNamespace(
                active_batch_ids=("batch-new", "batch-existing"),
                created_count=1,
                coalesced_count=1,
            )
        )
    )
    runtime = SimpleNamespace(
        subscription=SimpleNamespace(
            repository=lambda _db: repository,
            search_repository=search_repository,
        ),
    )
    command = subscription_dependencies.get_search_subscriptions_command(
        task_registry=registry,
        db=object(),
        runtime=runtime,
    )

    result = await command.execute(
        SubscribeSearchActor(username="alice", is_superuser=False)
    )

    assert result is not None
    assert result.batch_ids == ("batch-new", "batch-existing")
    assert result.queued_count == 1
    assert result.ongoing_count == 1
    assert calls[0] == (
        search_repository.enqueue,
        (),
        "api.subscribe.search.enqueue",
        {"subscription_ids": (2, 5), "source": "manual", "priority": 100},
    )
    assert calls[1] == (
        subscription_dependencies._resume_submitted_subscription_search,
        ((2, 5),),
        "api.subscribe.search.run",
        {},
    )
