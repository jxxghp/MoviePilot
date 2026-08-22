"""订阅删除应用用例的事务、权限与副作用时序测试。"""

from unittest.mock import AsyncMock

import pytest

from app.application.subscription.delete import (
    DeleteSubscribeCommand,
    SubscribeDeletionActor,
    SubscribeDeletionCandidate,
)
from app.db.models.subscribe import Subscribe
from app.db.oper.subscribe import SubscribeOper


class _Repository:
    """记录订阅删除用例数据访问顺序的仓储替身。"""

    def __init__(self, candidate, calls):
        """保存候选订阅和共享调用序列。"""
        self.candidate = candidate
        self.calls = calls

    async def get_candidate(self, subscribe_id):
        """返回预设候选订阅。"""
        self.calls.append(("get", subscribe_id))
        return self.candidate

    async def stage_delete(self, subscribe_id):
        """记录待删除的订阅编号。"""
        self.calls.append(("delete", subscribe_id))


class _UnitOfWork:
    """可注入提交异常的事务替身。"""

    def __init__(self, calls, commit_error=None):
        """保存共享调用序列与可选提交异常。"""
        self.calls = calls
        self.commit_error = commit_error

    async def commit(self):
        """记录提交并按需抛出异常。"""
        self.calls.append(("commit",))
        if self.commit_error:
            raise self.commit_error

    async def rollback(self):
        """记录回滚。"""
        self.calls.append(("rollback",))


class _Outbox:
    """记录订阅删除 intent 暂存和收口顺序的 outbox 替身。"""

    def __init__(self, calls, stage_error=None):
        """保存共享调用序列与可选暂存异常。"""
        self.calls = calls
        self.stage_error = stage_error

    async def stage(self, intent, _now):
        """记录 intent，并按需模拟持久化失败。"""
        self.calls.append(("outbox_stage", intent))
        if self.stage_error:
            raise self.stage_error

    async def complete_by_event_key(self, event_key, _completed_at):
        """记录即时事件成功后的 intent 收口。"""
        self.calls.append(("outbox_complete", event_key))


def _candidate(username="alice"):
    """构造带完整事件身份字段的订阅删除候选。"""
    return SubscribeDeletionCandidate(
        subscribe_id=7,
        username=username,
        event_payload={
            "id": 7,
            "username": username,
            "media_source": "tmdb",
            "media_id": "123",
            "season": 2,
            "name": "测试订阅",
        },
    )


def _command(
    candidate,
    calls,
    commit_error=None,
    event_error=None,
    report_error=None,
    outbox=None,
):
    """构造可观察事件与上报失败的订阅删除用例。"""
    async def publish(payload):
        """记录删除事件并按需失败。"""
        calls.append(("event", payload["subscribe_id"], payload))
        if event_error:
            raise event_error

    def report(payload):
        """记录删除统计并按需失败。"""
        calls.append(("report", payload))
        if report_error:
            raise report_error

    return DeleteSubscribeCommand(
        repository=_Repository(candidate, calls),
        unit_of_work=_UnitOfWork(calls, commit_error),
        publish_deleted=publish,
        report_deleted=report,
        outbox=outbox,
    )


@pytest.mark.asyncio
async def test_owner_delete_commits_before_event_and_report():
    """owner 删除成功时必须先提交，再按原顺序发送事件和上报。"""
    calls = []
    command = _command(_candidate(), calls)

    deleted = await command.execute(
        7,
        SubscribeDeletionActor(username="alice", is_superuser=False),
    )

    assert deleted is True
    assert [call[0] for call in calls] == ["get", "delete", "commit", "event", "report"]
    assert calls[3][2]["subscribe_info"] == _candidate().event_payload
    assert calls[3][2]["idempotency_key"].startswith("subscribe.deleted:7:")
    assert calls[4][1] == _candidate().event_payload


@pytest.mark.asyncio
@pytest.mark.parametrize("candidate", [None, _candidate("bob"), _candidate(None)])
async def test_regular_user_cannot_delete_missing_other_or_legacy_subscribe(candidate):
    """普通用户对不存在、他人和 legacy 订阅保持无痕成功语义。"""
    calls = []
    command = _command(candidate, calls)

    deleted = await command.execute(
        7,
        SubscribeDeletionActor(username="alice", is_superuser=False),
    )

    assert deleted is False
    assert calls == [("get", 7)]


@pytest.mark.asyncio
async def test_superuser_can_delete_other_users_subscribe():
    """超级用户保留全局订阅删除权限。"""
    calls = []
    command = _command(_candidate("bob"), calls)

    deleted = await command.execute(
        7,
        SubscribeDeletionActor(username="admin", is_superuser=True),
    )

    assert deleted is True
    assert [call[0] for call in calls] == ["get", "delete", "commit", "event", "report"]


@pytest.mark.asyncio
async def test_commit_failure_rolls_back_without_event_or_report():
    """提交失败必须回滚，且不得发送成功事件或统计上报。"""
    calls = []
    command = _command(_candidate(), calls, commit_error=RuntimeError("commit failed"))

    with pytest.raises(RuntimeError, match="commit failed"):
        await command.execute(
            7,
            SubscribeDeletionActor(username="alice", is_superuser=False),
        )

    assert [call[0] for call in calls] == ["get", "delete", "commit", "rollback"]


@pytest.mark.asyncio
async def test_event_failure_happens_after_commit_and_stops_report():
    """事件失败保持原有传播语义，但事务必须已经提交且不得继续上报。"""
    calls = []
    command = _command(_candidate(), calls, event_error=RuntimeError("event failed"))

    with pytest.raises(RuntimeError, match="event failed"):
        await command.execute(
            7,
            SubscribeDeletionActor(username="alice", is_superuser=False),
        )

    assert [call[0] for call in calls] == ["get", "delete", "commit", "event"]


@pytest.mark.asyncio
async def test_report_failure_happens_after_commit_and_event():
    """上报失败保持原有传播语义，且不得改变已经提交和发出的事件。"""
    calls = []
    command = _command(_candidate(), calls, report_error=RuntimeError("report failed"))

    with pytest.raises(RuntimeError, match="report failed"):
        await command.execute(
            7,
            SubscribeDeletionActor(username="alice", is_superuser=False),
        )

    assert [call[0] for call in calls] == ["get", "delete", "commit", "event", "report"]


@pytest.mark.asyncio
async def test_delete_stages_outbox_before_commit_and_completes_after_event():
    """订阅删除、intent 与即时事件必须按原子提交和成功收口顺序执行。"""
    calls = []
    command = _command(_candidate(), calls, outbox=_Outbox(calls))

    assert await command.execute(
        7,
        SubscribeDeletionActor(username="alice", is_superuser=False),
    ) is True

    assert [call[0] for call in calls] == [
        "get",
        "delete",
        "outbox_stage",
        "commit",
        "event",
        "outbox_complete",
        "report",
    ]
    intent = calls[2][1]
    assert intent.topic == "subscribe.deleted"
    assert intent.event_key == calls[4][2]["idempotency_key"]
    assert calls[5][1] == intent.event_key


@pytest.mark.asyncio
async def test_delete_outbox_stage_failure_rolls_back_business_delete():
    """订阅删除 intent 无法暂存时不得提交业务删除。"""
    calls = []
    command = _command(
        _candidate(),
        calls,
        outbox=_Outbox(calls, stage_error=RuntimeError("outbox failed")),
    )

    with pytest.raises(RuntimeError, match="outbox failed"):
        await command.execute(
            7,
            SubscribeDeletionActor(username="alice", is_superuser=False),
        )

    assert [call[0] for call in calls] == [
        "get",
        "delete",
        "outbox_stage",
        "rollback",
    ]


@pytest.mark.asyncio
async def test_repository_candidate_uses_loaded_orm_snapshot(monkeypatch):
    """DB 仓库只向调用方暴露持久化字段字典，不构造应用层业务对象。"""
    subscribe = Subscribe(
        id=7,
        username="alice",
        name="测试订阅",
        media_source="tmdb",
        media_id="123",
        season=2,
    )

    async def async_get(_self, subscribe_id):
        """返回无需真实数据库的订阅模型。"""
        assert subscribe_id == 7
        return subscribe

    monkeypatch.setattr(SubscribeOper, "async_get", async_get)

    row = await SubscribeOper(object()).get_candidate(7)

    assert row is not None
    assert row["subscribe_id"] == 7
    assert row["username"] == "alice"
    assert row["event_payload"]["id"] == 7
    assert row["event_payload"]["media_source"] == "tmdb"
    assert row["event_payload"]["media_id"] == "123"


@pytest.mark.asyncio
async def test_repository_stage_delete_does_not_commit():
    """真实仓储只登记删除，提交必须由请求级 UnitOfWork 执行。"""
    session = type("SessionStub", (), {})()
    session.execute = AsyncMock()
    session.commit = AsyncMock()

    await SubscribeOper(session).stage_delete(7)

    session.execute.assert_awaited_once()
    session.commit.assert_not_awaited()
