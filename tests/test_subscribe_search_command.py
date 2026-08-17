import pytest

from app.application.subscription.delete import SubscribeDeletionCandidate
from app.application.subscription.search import (
    SearchSubscriptionsCommand,
    SubscribeSearchActor,
)


class _Repository:
    """提供手工订阅搜索测试需要的归属和列表数据。"""

    def __init__(self, candidate=None, subscribe_ids=None):
        """保存预设单条候选和批量编号。"""
        self.candidate = candidate
        self.subscribe_ids = subscribe_ids or []

    async def get_candidate(self, _subscribe_id):
        """返回预设订阅候选。"""
        return self.candidate

    async def list_search_ids(self, username, state):
        """校验普通用户搜索状态并返回预设编号。"""
        assert username == "alice"
        assert state == "R"
        return self.subscribe_ids


def _candidate(username):
    """构造只包含归属信息的订阅候选。"""
    return SubscribeDeletionCandidate(
        subscribe_id=7,
        username=username,
        event_payload={},
    )


@pytest.mark.asyncio
async def test_superuser_search_all_uses_single_global_scheduler_request():
    """管理员搜索全部订阅时保持一次 state=R 的全局调度语义。"""
    scheduled = []
    command = SearchSubscriptionsCommand(
        repository=_Repository(),
        schedule_search=lambda sid, state: scheduled.append((sid, state)),
    )

    assert await command.execute(
        SubscribeSearchActor(username="admin", is_superuser=True)
    ) is True
    assert scheduled == [(None, "R")]


@pytest.mark.asyncio
async def test_regular_user_search_all_schedules_only_owned_subscriptions():
    """普通用户搜索全部时逐条提交仓储已按归属过滤的订阅。"""
    scheduled = []
    command = SearchSubscriptionsCommand(
        repository=_Repository(subscribe_ids=[2, 5]),
        schedule_search=lambda sid, state: scheduled.append((sid, state)),
    )

    assert await command.execute(
        SubscribeSearchActor(username="alice", is_superuser=False)
    ) is True
    assert scheduled == [(2, None), (5, None)]


@pytest.mark.asyncio
async def test_targeted_search_rejects_missing_or_other_users_subscription():
    """单条搜索不得泄漏订阅是否属于其他普通用户。"""
    scheduled = []
    command = SearchSubscriptionsCommand(
        repository=_Repository(candidate=_candidate("bob")),
        schedule_search=lambda sid, state: scheduled.append((sid, state)),
    )

    assert await command.execute(
        SubscribeSearchActor(username="alice", is_superuser=False),
        subscribe_id=7,
    ) is False
    assert scheduled == []


@pytest.mark.asyncio
async def test_targeted_search_schedules_accessible_subscription():
    """归属用户搜索单条订阅时提交历史兼容参数。"""
    scheduled = []
    command = SearchSubscriptionsCommand(
        repository=_Repository(candidate=_candidate("alice")),
        schedule_search=lambda sid, state: scheduled.append((sid, state)),
    )

    assert await command.execute(
        SubscribeSearchActor(username="alice", is_superuser=False),
        subscribe_id=7,
    ) is True
    assert scheduled == [(7, None)]
