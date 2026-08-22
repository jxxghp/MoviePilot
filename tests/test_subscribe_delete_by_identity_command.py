"""按媒体身份批量删除订阅的应用用例测试。"""

import pytest

from app.application.subscription.delete import (
    SubscribeDeletionActor,
    SubscribeDeletionCandidate,
)
from app.application.subscription.identity import (
    DeleteSubscriptionsByIdentityCommand,
)
from app.schemas.types import MediaSource


class _Repository:
    """记录批量订阅删除顺序的仓储替身。"""

    def __init__(self, candidates, calls):
        """保存候选订阅和共享调用序列。"""
        self.candidates = candidates
        self.calls = calls

    async def list_candidates_by_identity(self, *args):
        """记录媒体身份查询参数并返回候选订阅。"""
        self.calls.append(("list", *args))
        return self.candidates

    async def stage_delete(self, subscribe_id):
        """记录待删除订阅。"""
        self.calls.append(("delete", subscribe_id))


class _UnitOfWork:
    """可注入提交异常的批量事务替身。"""

    def __init__(self, calls, commit_error=None):
        """保存共享调用序列与可选提交异常。"""
        self.calls = calls
        self.commit_error = commit_error

    async def commit(self):
        """记录提交并按需失败。"""
        self.calls.append(("commit",))
        if self.commit_error:
            raise self.commit_error

    async def rollback(self):
        """记录回滚。"""
        self.calls.append(("rollback",))


def _candidate(subscribe_id, username):
    """构造批量删除候选订阅。"""
    return SubscribeDeletionCandidate(
        subscribe_id=subscribe_id,
        username=username,
        event_payload={
            "id": subscribe_id,
            "username": username,
            "media_source": "tmdb",
            "media_id": "123",
        },
    )


def _command(candidates, calls, commit_error=None, failing_event_id=None):
    """构造带可观察事件错误处理的批量删除用例。"""
    async def publish(payload):
        """记录事件并按订阅编号注入失败。"""
        subscribe_id = payload["subscribe_id"]
        calls.append(("event", subscribe_id, payload))
        if subscribe_id == failing_event_id:
            raise RuntimeError("event failed")

    def handle_error(subscribe_id, error):
        """记录被隔离的单条事件异常。"""
        calls.append(("event_error", subscribe_id, str(error)))

    return DeleteSubscriptionsByIdentityCommand(
        repository=_Repository(candidates, calls),
        unit_of_work=_UnitOfWork(calls, commit_error),
        publish_deleted=publish,
        handle_event_error=handle_error,
    )


@pytest.mark.asyncio
async def test_bulk_delete_filters_owner_and_commits_before_events():
    """普通用户只删除自己的候选，并在提交后发送事件。"""
    calls = []
    command = _command([_candidate(1, "bob"), _candidate(2, "alice")], calls)

    deleted = await command.execute(
        MediaSource.TMDB,
        "123",
        1,
        None,
        SubscribeDeletionActor(username="alice", is_superuser=False),
    )

    assert deleted == 1
    assert [call[0] for call in calls] == ["list", "delete", "commit", "event"]
    assert calls[1] == ("delete", 2)


@pytest.mark.asyncio
async def test_bulk_delete_commits_even_when_nothing_matches():
    """无匹配订阅时仍保持历史上的空事务提交行为。"""
    calls = []
    command = _command([], calls)

    deleted = await command.execute(
        MediaSource.TMDB,
        "123",
        None,
        None,
        SubscribeDeletionActor(username="alice", is_superuser=False),
    )

    assert deleted == 0
    assert [call[0] for call in calls] == ["list", "commit"]


@pytest.mark.asyncio
async def test_bulk_delete_commit_failure_rolls_back_without_events():
    """批量提交失败必须回滚且不发送任何删除事件。"""
    calls = []
    command = _command(
        [_candidate(1, "alice")],
        calls,
        commit_error=RuntimeError("commit failed"),
    )

    with pytest.raises(RuntimeError, match="commit failed"):
        await command.execute(
            MediaSource.TMDB,
            "123",
            None,
            None,
            SubscribeDeletionActor(username="alice", is_superuser=False),
        )

    assert [call[0] for call in calls] == ["list", "delete", "commit", "rollback"]


@pytest.mark.asyncio
async def test_bulk_delete_isolates_one_event_failure_and_continues():
    """单条事件失败只记录错误，后续已提交订阅仍继续发事件。"""
    calls = []
    command = _command(
        [_candidate(1, "alice"), _candidate(2, "alice")],
        calls,
        failing_event_id=1,
    )

    deleted = await command.execute(
        MediaSource.TMDB,
        "123",
        None,
        None,
        SubscribeDeletionActor(username="alice", is_superuser=False),
    )

    assert deleted == 2
    assert [call[0] for call in calls] == [
        "list",
        "delete",
        "delete",
        "commit",
        "event",
        "event_error",
        "event",
    ]
