"""durable side-effect outbox 原子性、认领、重试与幂等测试。"""

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from app.application.outbox import ClaimedOutboxMessage, OutboxDispatcher
from app.application.subscription.write import CreateSubscriptionCommand


class _Staged:
    """测试用新订阅暂存结果。"""

    subscribe_id = 42
    message = "ok"
    created = True


def test_subscription_and_outbox_intent_commit_together() -> None:
    """业务行与 intent 均 stage 成功后才允许同一次 commit。"""
    calls = []
    repository = MagicMock()
    repository.stage_add.side_effect = lambda *_args: calls.append("subscription") or _Staged()
    outbox = MagicMock()
    outbox.stage.side_effect = lambda *_args: calls.append("outbox")
    unit_of_work = MagicMock()
    unit_of_work.commit.side_effect = lambda: calls.append("commit")
    command = CreateSubscriptionCommand(repository, unit_of_work, outbox=outbox)

    result = command.execute({}, {"name": "demo"}, "user")

    assert result == (42, "ok")
    assert calls == ["subscription", "outbox", "commit"]
    intent = outbox.stage.call_args.args[0]
    assert intent.event_key == "subscribe.added:42:unknown:unknown:v1"
    assert intent.payload["subscribe_id"] == 42


def test_outbox_stage_failure_rolls_back_business_transaction() -> None:
    """intent 无法持久化时订阅行不得单独提交。"""
    repository = MagicMock()
    repository.stage_add.return_value = _Staged()
    outbox = MagicMock()
    outbox.stage.side_effect = RuntimeError("outbox unavailable")
    unit_of_work = MagicMock()
    command = CreateSubscriptionCommand(repository, unit_of_work, outbox=outbox)

    with pytest.raises(RuntimeError, match="outbox unavailable"):
        command.execute({}, {"name": "demo"})

    unit_of_work.rollback.assert_called_once_with()
    unit_of_work.commit.assert_not_called()


def test_dispatcher_retries_then_dead_letters_with_stable_key() -> None:
    """同一幂等键有限指数退避，达到上限后进入 dead letter。"""
    now = datetime(2026, 8, 21, tzinfo=timezone.utc)
    repository = MagicMock()
    repository.claim.side_effect = [
        ClaimedOutboxMessage(1, "subscribe.added:42:v1", "subscribe.added", {}, 1, 1),
        ClaimedOutboxMessage(1, "subscribe.added:42:v1", "subscribe.added", {}, 1, 2),
    ]
    handler = MagicMock(side_effect=RuntimeError("temporary"))
    failure_observer = MagicMock()
    dispatcher = OutboxDispatcher(
        repository,
        {"subscribe.added": handler},
        max_attempts=2,
        clock=lambda: now,
        failure_observer=failure_observer,
    )

    assert dispatcher.dispatch_one() is True
    assert repository.retry.call_args.kwargs["dead"] is False
    assert dispatcher.dispatch_one() is True
    assert repository.retry.call_args.kwargs["dead"] is True
    assert [call.args[0].event_key for call in handler.call_args_list] == [
        "subscribe.added:42:v1",
        "subscribe.added:42:v1",
    ]
    assert [call.args[0] for call in failure_observer.call_args_list] == [
        False,
        True,
    ]


def test_dispatcher_marks_success_and_closes_owned_resource() -> None:
    """成功 handler 收口消息，批次结束释放 Session 所有权。"""
    now = datetime(2026, 8, 21, tzinfo=timezone.utc)
    repository = MagicMock()
    message = ClaimedOutboxMessage(7, "key", "subscribe.added", {}, 1, 1)
    repository.claim.return_value = message
    close = MagicMock()
    dispatcher = OutboxDispatcher(
        repository,
        {"subscribe.added": MagicMock()},
        clock=lambda: now,
        close=close,
    )

    assert dispatcher.dispatch_one() is True
    repository.complete.assert_called_once_with(7, now)
    dispatcher.close()
    close.assert_called_once_with()
