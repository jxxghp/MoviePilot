"""消息队列同步与异步入口的调度决策一致性测试。"""

import asyncio
import queue
from unittest.mock import Mock, call

import pytest

from app.application.messaging.message import MessageQueueManager


@pytest.fixture
def message_queue(monkeypatch: pytest.MonkeyPatch) -> MessageQueueManager:
    """构造不启动监控线程的独立消息队列。"""
    manager = object.__new__(MessageQueueManager)
    manager.queue = queue.Queue()
    manager.send_callback = Mock()
    monkeypatch.setattr(manager, "_is_in_scheduled_time", lambda _now: False)
    return manager


@pytest.mark.parametrize(
    ("immediately", "scheduled", "expected"),
    [
        (False, False, False),
        (False, True, True),
        (True, False, True),
        (True, True, True),
    ],
)
def test_dispatch_plan_combines_immediate_and_schedule_decisions(
    immediately: bool, scheduled: bool, expected: bool
) -> None:
    """共享计划应唯一决定立即发送结论并移除内部控制参数。"""
    callback = Mock()

    send_now, message = MessageQueueManager._build_message_dispatch(
        send_callback=callback,
        explicit_callback=True,
        args=("payload",),
        kwargs={"immediately": immediately, "token": 1},
        scheduled=scheduled,
    )

    assert send_now is expected
    assert message == {
        "args": ("payload",),
        "kwargs": {"token": 1},
        "send_callback": callback,
    }


def test_default_queue_payload_matches_between_sync_and_async(
    message_queue: MessageQueueManager,
) -> None:
    """默认回调双入口在免打扰时段应生成完全相同的队列载荷。"""
    message_queue.send_message("payload", token=1)
    asyncio.run(message_queue.async_send_message("payload", token=1))

    sync_message = message_queue.queue.get_nowait()
    async_message = message_queue.queue.get_nowait()

    assert sync_message == async_message == {
        "args": ("payload",),
        "kwargs": {"token": 1},
    }
    message_queue.send_callback.assert_not_called()


def test_explicit_queue_payload_matches_between_sync_and_async(
    message_queue: MessageQueueManager,
) -> None:
    """显式回调双入口排队时都应保留各消息自己的回调身份。"""
    callback = Mock()

    message_queue.send_message_for(callback, "payload", token=1)
    asyncio.run(
        message_queue.async_send_message_for(callback, "payload", token=1)
    )

    sync_message = message_queue.queue.get_nowait()
    async_message = message_queue.queue.get_nowait()

    assert sync_message == async_message == {
        "args": ("payload",),
        "kwargs": {"token": 1},
        "send_callback": callback,
    }
    callback.assert_not_called()


def test_immediate_delivery_matches_between_sync_and_async(
    message_queue: MessageQueueManager,
) -> None:
    """强制立即发送时双入口都应绕过队列并传递相同业务参数。"""
    callback = message_queue.send_callback

    message_queue.send_message("payload", token=1, immediately=True)
    asyncio.run(
        message_queue.async_send_message("payload", token=1, immediately=True)
    )

    assert callback.call_args_list == [
        call("payload", token=1),
        call("payload", token=1),
    ]
    assert message_queue.queue.empty()


def test_explicit_immediate_delivery_matches_between_sync_and_async(
    message_queue: MessageQueueManager,
) -> None:
    """显式回调强制发送时双入口都应绕过队列并移除控制参数。"""
    callback = Mock()

    message_queue.send_message_for(
        callback, "payload", token=1, immediately=True
    )
    asyncio.run(
        message_queue.async_send_message_for(
            callback, "payload", token=1, immediately=True
        )
    )

    assert callback.call_args_list == [
        call("payload", token=1),
        call("payload", token=1),
    ]
    assert message_queue.queue.empty()
