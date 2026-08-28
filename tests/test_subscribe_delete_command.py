"""订阅删除应用用例的事务、权限与副作用时序测试。"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.application.outbox import ClaimedOutboxMessage
from app.application.subscription.delete import (
    DeleteSubscribeCommand,
    SubscribeDeletionActor,
    SubscribeDeletionCandidate,
    SyncDeleteSubscribeCommand,
)
from app.db.models.subscribe import Subscribe
from app.db.oper.subscribe import SubscribeOper


class _Repository:
    """记录订阅删除用例数据访问顺序的仓储替身。"""

    def __init__(self, candidate, calls, delete_error=None):
        """保存候选订阅、共享调用序列和可选删除异常。"""
        self.candidate = candidate
        self.calls = calls
        self.delete_error = delete_error

    async def get_candidate(self, subscribe_id):
        """返回预设候选订阅。"""
        self.calls.append(("get", subscribe_id))
        return self.candidate

    async def stage_delete(self, subscribe_id):
        """记录待删除的订阅编号。"""
        self.calls.append(("delete", subscribe_id))
        if self.delete_error:
            raise self.delete_error


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

    async def claim_by_event_key(self, event_key, _now, _lease_until):
        """记录并返回当前测试拥有的异步 lease。"""
        self.calls.append(("outbox_claim", event_key))
        return ClaimedOutboxMessage(
            message_id=len(self.calls),
            event_key=event_key,
            topic="test",
            payload={},
            payload_version=1,
            attempt=1,
        )

    async def complete(self, message_id, attempt, _completed_at):
        """记录带 attempt fencing 的异步完成。"""
        self.calls.append(("outbox_complete", message_id, attempt))
        return True

    async def retry(self, message_id, attempt, **_kwargs):
        """记录带 attempt fencing 的异步重试。"""
        self.calls.append(("outbox_retry", message_id, attempt))
        return True


class _SyncRepository:
    """记录同步订阅删除的数据访问顺序。"""

    def __init__(self, candidate, calls, delete_error=None):
        """保存候选订阅、共享调用序列和可选删除异常。"""
        self.candidate = candidate
        self.calls = calls
        self.delete_error = delete_error

    def get_candidate_sync(self, subscribe_id):
        """返回预设候选订阅。"""
        self.calls.append(("get", subscribe_id))
        return self.candidate

    def stage_delete_sync(self, subscribe_id):
        """记录同步待删除编号并按需失败。"""
        self.calls.append(("delete", subscribe_id))
        if self.delete_error:
            raise self.delete_error


class _SyncUnitOfWork:
    """记录同步删除命令的提交与回滚。"""

    def __init__(self, calls, commit_error=None):
        """保存共享调用序列与可选提交异常。"""
        self.calls = calls
        self.commit_error = commit_error

    def commit(self):
        """记录提交并按需抛出异常。"""
        self.calls.append(("commit",))
        if self.commit_error:
            raise self.commit_error

    def rollback(self):
        """记录回滚。"""
        self.calls.append(("rollback",))


class _SyncOutbox:
    """记录同步删除 intent 的暂存与完成顺序。"""

    def __init__(self, calls):
        """保存共享调用序列。"""
        self.calls = calls

    def stage(self, intent, _now):
        """记录同步暂存的 intent。"""
        self.calls.append(("outbox_stage", intent))

    def claim_by_event_key(self, event_key, _now, _lease_until):
        """记录并返回当前测试拥有的同步 lease。"""
        self.calls.append(("outbox_claim", event_key))
        return ClaimedOutboxMessage(
            message_id=len(self.calls),
            event_key=event_key,
            topic="test",
            payload={},
            payload_version=1,
            attempt=1,
        )

    def complete(self, message_id, attempt, _completed_at):
        """记录带 attempt fencing 的同步完成。"""
        self.calls.append(("outbox_complete", message_id, attempt))
        return True

    def retry(self, message_id, attempt, **_kwargs):
        """记录带 attempt fencing 的同步重试。"""
        self.calls.append(("outbox_retry", message_id, attempt))
        return True


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
    delete_error=None,
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
        return True

    return DeleteSubscribeCommand(
        repository=_Repository(candidate, calls, delete_error),
        unit_of_work=_UnitOfWork(calls, commit_error),
        publish_deleted=publish,
        report_deleted=report,
        outbox=outbox,
        dispatch_store=outbox,
    )


def _async_report_command(candidate, calls, result=True, error=None, outbox=None):
    """构造异步统计 reporter，验证命令可等待真实远端确认。"""
    async def publish(payload):
        """记录删除事件。"""
        calls.append(("event", payload["subscribe_id"], payload))

    async def report(payload):
        """记录异步统计并按需返回未确认或抛错。"""
        calls.append(("report", payload))
        if error:
            raise error
        return result

    return DeleteSubscribeCommand(
        repository=_Repository(candidate, calls),
        unit_of_work=_UnitOfWork(calls),
        publish_deleted=publish,
        report_deleted=report,
        outbox=outbox,
        dispatch_store=outbox,
    )


def _sync_command(
    candidate,
    calls,
    commit_error=None,
    delete_error=None,
    outbox=None,
    report_result=True,
):
    """构造可观察事务和副作用顺序的同步订阅删除命令。"""
    def publish(payload):
        """记录同步删除事件。"""
        calls.append(("event", payload["subscribe_id"], payload))

    def report(payload):
        """记录同步删除统计。"""
        calls.append(("report", payload))
        return report_result

    return SyncDeleteSubscribeCommand(
        repository=_SyncRepository(candidate, calls, delete_error),
        unit_of_work=_SyncUnitOfWork(calls, commit_error),
        publish_deleted=publish,
        report_deleted=report,
        outbox=outbox,
        dispatch_store=outbox,
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
    report_payload = dict(calls[4][1])
    assert report_payload.pop("idempotency_key").endswith(":report")
    assert report_payload == _candidate().event_payload


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
async def test_delete_stage_failure_rolls_back_without_effects():
    """异步暂存失败必须显式回滚，且不得写 intent 或发送成功副作用。"""
    calls = []
    command = _command(
        _candidate(),
        calls,
        delete_error=RuntimeError("delete failed"),
        outbox=_Outbox(calls),
    )

    with pytest.raises(RuntimeError, match="delete failed"):
        await command.execute(
            7,
            SubscribeDeletionActor(username="alice", is_superuser=False),
        )

    assert [call[0] for call in calls] == ["get", "delete", "rollback"]


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
    """上报异常不得把已经提交的删除误报为失败。"""
    calls = []
    command = _command(_candidate(), calls, report_error=RuntimeError("report failed"))

    assert await command.execute(
        7,
        SubscribeDeletionActor(username="alice", is_superuser=False),
    ) is True

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
        "outbox_stage",
        "commit",
        "outbox_claim",
        "event",
        "outbox_complete",
        "outbox_claim",
        "report",
        "outbox_complete",
    ]
    intent = calls[2][1]
    assert intent.topic == "subscribe.deleted"
    report_intent = calls[3][1]
    assert report_intent.topic == "subscribe.deleted.report"
    assert intent.event_key == calls[6][2]["idempotency_key"]
    assert calls[5][1] == intent.event_key
    assert calls[8][1] == report_intent.event_key


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
async def test_async_reporter_completes_report_intent_only_after_confirmation():
    """异步 reporter 确认成功后才允许收口统计 intent。"""
    calls = []
    command = _async_report_command(_candidate(), calls, outbox=_Outbox(calls))

    assert await command.execute(
        7,
        SubscribeDeletionActor(username="alice", is_superuser=False),
    ) is True

    assert [call[0] for call in calls] == [
        "get", "delete", "outbox_stage", "outbox_stage", "commit",
        "outbox_claim", "event", "outbox_complete", "outbox_claim",
        "report", "outbox_complete",
    ]


@pytest.mark.asyncio
async def test_async_reporter_false_keeps_report_intent_pending():
    """异步 reporter 未确认时返回成功并保留待重试统计 intent。"""
    calls = []
    command = _async_report_command(_candidate(), calls, result=False, outbox=_Outbox(calls))

    assert await command.execute(
        7,
        SubscribeDeletionActor(username="alice", is_superuser=False),
    ) is True

    assert [call[0] for call in calls] == [
        "get", "delete", "outbox_stage", "outbox_stage", "commit",
        "outbox_claim", "event", "outbox_complete", "outbox_claim",
        "report", "outbox_retry",
    ]


@pytest.mark.asyncio
async def test_async_reporter_error_keeps_report_intent_pending():
    """异步 reporter 异常时返回成功并保留待重试统计 intent。"""
    calls = []
    command = _async_report_command(
        _candidate(),
        calls,
        error=RuntimeError("remote failed"),
        outbox=_Outbox(calls),
    )

    assert await command.execute(
        7,
        SubscribeDeletionActor(username="alice", is_superuser=False),
    ) is True

    assert [call[0] for call in calls] == [
        "get", "delete", "outbox_stage", "outbox_stage", "commit",
        "outbox_claim", "event", "outbox_complete", "outbox_claim",
        "report", "outbox_retry",
    ]


@pytest.mark.asyncio
async def test_repository_candidate_uses_loaded_orm_snapshot(monkeypatch):
    """DB 适配器只向应用层暴露权限字段和完整列快照。"""
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

    candidate = await SubscribeOper(object()).get_candidate(7)

    assert candidate is not None
    assert candidate.subscribe_id == 7
    assert candidate.username == "alice"
    assert candidate.event_payload["id"] == 7
    assert candidate.event_payload["media_source"] == "tmdb"
    assert candidate.event_payload["media_id"] == "123"


@pytest.mark.asyncio
async def test_repository_stage_delete_does_not_commit():
    """真实仓储只登记删除，提交必须由请求级 UnitOfWork 执行。"""
    session = MagicMock(spec=AsyncSession)
    session.execute = AsyncMock()
    session.commit = AsyncMock()

    await SubscribeOper(session).stage_delete(7)

    session.execute.assert_awaited_once()
    session.commit.assert_not_awaited()


def test_sync_delete_uses_same_durable_effect_order():
    """同步消息入口必须复用异步删除命令的事务、事件和统计顺序。"""
    calls = []
    command = _sync_command(_candidate(), calls, outbox=_SyncOutbox(calls))

    assert command.execute(
        7,
        SubscribeDeletionActor(username="", is_superuser=True),
    ) is True

    assert [call[0] for call in calls] == [
        "get", "delete", "outbox_stage", "outbox_stage", "commit",
        "outbox_claim", "event", "outbox_complete", "outbox_claim",
        "report", "outbox_complete",
    ]
    assert calls[2][1].topic == "subscribe.deleted"
    assert calls[3][1].topic == "subscribe.deleted.report"


def test_sync_delete_report_failure_returns_success_and_keeps_intent_pending():
    """同步删除统计未确认时仍返回成功，且不收口 report intent。"""
    calls = []
    command = _sync_command(
        _candidate(),
        calls,
        outbox=_SyncOutbox(calls),
        report_result=False,
    )

    assert command.execute(
        7,
        SubscribeDeletionActor(username="alice", is_superuser=False),
    ) is True
    assert [call[0] for call in calls] == [
        "get", "delete", "outbox_stage", "outbox_stage", "commit",
        "outbox_claim", "event", "outbox_complete", "outbox_claim",
        "report", "outbox_retry",
    ]
    report_payload = dict(calls[9][1])
    assert report_payload.pop("idempotency_key").endswith(":report")
    assert report_payload == _candidate().event_payload


@pytest.mark.parametrize("failure", ["delete", "commit"])
def test_sync_delete_rolls_back_transaction_failures(failure):
    """同步暂存或提交失败时必须回滚，且不得发送删除成功副作用。"""
    calls = []
    error = RuntimeError(f"{failure} failed")
    command = _sync_command(
        _candidate(),
        calls,
        delete_error=error if failure == "delete" else None,
        commit_error=error if failure == "commit" else None,
    )

    with pytest.raises(RuntimeError, match=f"{failure} failed"):
        command.execute(
            7,
            SubscribeDeletionActor(username="", is_superuser=True),
        )

    assert calls[-1] == ("rollback",)
    assert all(call[0] not in {"event", "report"} for call in calls)


def test_sync_repository_candidate_and_delete_share_caller_session(monkeypatch):
    """同步仓储投影和删除都使用组合根传入的同一个 Session 且不提交。"""
    subscribe = Subscribe(
        id=7,
        username="alice",
        name="测试订阅",
        media_source="tmdb",
        media_id="123",
        season=2,
    )
    session = MagicMock(spec=Session)
    oper = SubscribeOper(session)
    monkeypatch.setattr(oper, "get", lambda subscribe_id: subscribe)

    candidate = oper.get_candidate_sync(7)
    oper.stage_delete_sync(7)

    assert candidate is not None
    assert candidate.event_payload["media_id"] == "123"
