"""订阅修改 UoW 与 durable outbox 边界测试。"""

from dataclasses import replace

import pytest

from app.application.outbox import ClaimedOutboxMessage
from app.application.subscription.contract import SubscriptionPatch, SubscriptionSnapshot
from app.application.subscription.mutation import (
    SubscriptionActor,
    SubscriptionMutationService,
)


class _Repository:
    """记录订阅读取、兼容更新和事务内暂存顺序。"""

    def __init__(self, subscribe: SubscriptionSnapshot, calls: list) -> None:
        """保存订阅对象与共享调用序列。"""
        self.subscribe = subscribe
        self.calls = calls

    async def async_get(self, subscribe_id: int):
        """返回指定订阅。"""
        self.calls.append(("get", subscribe_id))
        return self.subscribe

    async def async_update(self, subscribe_id: int, payload: SubscriptionPatch):
        """模拟旧兼容自动提交路径。"""
        self.calls.append(("legacy_update", subscribe_id, payload))
        self.subscribe = replace(self.subscribe, **payload.to_payload())
        return self.subscribe

    async def async_stage_update(self, subscribe_id: int, payload: SubscriptionPatch) -> SubscriptionSnapshot:
        """模拟调用方事务内的更新暂存。"""
        self.calls.append(("stage_update", subscribe_id, payload))
        self.subscribe = replace(self.subscribe, **payload.to_payload())
        return self.subscribe

    def get(self, subscribe_id: int):
        """提供协议要求的同步读取。"""
        return self.subscribe if subscribe_id == self.subscribe.id else None


class _UnitOfWork:
    """记录订阅修改事务提交和回滚。"""

    def __init__(self, calls: list) -> None:
        """保存共享调用序列。"""
        self.calls = calls

    async def commit(self) -> None:
        """记录提交。"""
        self.calls.append(("commit",))

    async def rollback(self) -> None:
        """记录回滚。"""
        self.calls.append(("rollback",))


class _Outbox:
    """记录修改事件 intent 暂存和完成。"""

    def __init__(self, calls: list, stage_error: Exception | None = None) -> None:
        """保存共享调用序列与可选暂存异常。"""
        self.calls = calls
        self.stage_error = stage_error

    async def stage(self, intent, _now) -> None:
        """记录 intent 并按需失败。"""
        self.calls.append(("outbox_stage", intent))
        if self.stage_error:
            raise self.stage_error

    async def claim_by_event_key(self, event_key, _now, _lease_until):
        """记录并返回当前测试拥有的派发 lease。"""
        self.calls.append(("outbox_claim", event_key))
        return ClaimedOutboxMessage(
            message_id=7,
            event_key=event_key,
            topic="subscribe.modified",
            payload={},
            payload_version=1,
            attempt=1,
        )

    async def complete(self, message_id, attempt, _completed_at):
        """记录带 attempt fencing 的完成结算。"""
        self.calls.append(("outbox_complete", message_id, attempt))
        return True

    async def retry(self, message_id, attempt, **_kwargs):
        """记录带 attempt fencing 的失败释放。"""
        self.calls.append(("outbox_retry", message_id, attempt))
        return True


def _service(calls: list, *, event_error: Exception | None = None, outbox=None):
    """构造拥有请求级 UoW 和 outbox 的订阅修改服务。"""
    subscribe = SubscriptionSnapshot(
        id=7,
        username="alice",
        name="旧标题",
    )

    async def publish(payload: dict) -> None:
        """记录公开事件并按需失败。"""
        calls.append(("event", payload))
        if event_error:
            raise event_error

    outbox = outbox or _Outbox(calls)
    return SubscriptionMutationService(
        repository=_Repository(subscribe, calls),
        unit_of_work=_UnitOfWork(calls),
        outbox=outbox,
        dispatch_store=outbox,
        publish_modified=publish,
    )


@pytest.mark.asyncio
async def test_modified_event_is_staged_with_update_and_completed_after_publish():
    """订阅修改与 intent 同事务提交，事件成功后才标记完成。"""
    calls = []
    service = _service(calls)

    change = await service.update(
        7,
        {"name": "新标题"},
        SubscriptionActor(name="alice", is_superuser=False),
        scene="update",
    )

    assert change is not None
    assert change.event_published is True
    assert change.old["name"] == "旧标题"
    assert change.new["name"] == "新标题"
    assert [call[0] for call in calls] == [
        "get",
        "stage_update",
        "outbox_stage",
        "commit",
        "outbox_claim",
        "event",
        "outbox_complete",
    ]
    intent = calls[2][1]
    assert intent.topic == "subscribe.modified"
    assert intent.event_key.startswith("subscribe.modified:7:update:")
    assert calls[5][1]["idempotency_key"] == intent.event_key
    assert calls[6][1:] == (7, 1)


@pytest.mark.asyncio
async def test_modified_outbox_stage_failure_rolls_back_update():
    """修改事件 intent 无法暂存时业务更新不得提交。"""
    calls = []
    service = _service(
        calls,
        outbox=_Outbox(calls, stage_error=RuntimeError("outbox failed")),
    )

    with pytest.raises(RuntimeError, match="outbox failed"):
        await service.update(
            7,
            {"name": "新标题"},
            SubscriptionActor(name="alice", is_superuser=False),
        )

    assert [call[0] for call in calls] == [
        "get",
        "stage_update",
        "outbox_stage",
        "rollback",
    ]


@pytest.mark.asyncio
async def test_modified_event_failure_keeps_committed_intent_pending():
    """提交后的事件失败向调用方传播，且不得错误收口待恢复 intent。"""
    calls = []
    service = _service(calls, event_error=RuntimeError("event failed"))

    with pytest.raises(RuntimeError, match="event failed"):
        await service.update(
            7,
            {"name": "新标题"},
            SubscriptionActor(name="alice", is_superuser=False),
        )

    assert [call[0] for call in calls] == [
        "get",
        "stage_update",
        "outbox_stage",
        "commit",
        "outbox_claim",
        "event",
        "outbox_retry",
    ]
