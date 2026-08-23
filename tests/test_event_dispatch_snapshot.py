"""事件调度订阅快照和生命周期回归测试。"""

import asyncio
import threading

import pytest

from app.runtime.config import global_vars
from app.runtime import events as events_module
from app.runtime.events import Event, eventmanager
from app.schemas.types import ChainEventType, EventType


class _ImmediateExecutor:
    """在当前线程执行广播 handler，使订阅变更精确发生在调度迭代期间。"""

    @staticmethod
    def submit(func, *args, **kwargs):
        return func(*args, **kwargs)


@pytest.fixture
def isolated_eventmanager(monkeypatch):
    """隔离全局事件总线的订阅表和广播执行器。"""
    monkeypatch.setattr(
        eventmanager,
        "_EventManager__broadcast_subscribers",
        {},
    )
    monkeypatch.setattr(
        eventmanager,
        "_EventManager__chain_subscribers",
        {},
    )
    monkeypatch.setattr(
        eventmanager,
        "_EventManager__handler_instance_resolvers",
        {},
    )
    monkeypatch.setattr(
        eventmanager,
        "_EventManager__executor",
        _ImmediateExecutor(),
    )
    monkeypatch.setattr(
        eventmanager,
        "_EventManager__event",
        threading.Event(),
    )
    monkeypatch.setattr(
        eventmanager,
        "_EventManager__consumer_threads",
        [],
    )
    monkeypatch.setattr(
        eventmanager,
        "_EventManager__lifecycle_state",
        "new",
    )
    monkeypatch.setattr(
        eventmanager,
        "_EventManager__async_handles",
        {},
    )
    return eventmanager


def test_broadcast_dispatch_uses_subscription_snapshot(isolated_eventmanager):
    """广播事件中新增或移除的 handler 从下一个事件开始生效。"""
    calls = []

    def late_handler(_event):
        calls.append("late")

    def removed_handler(_event):
        calls.append("removed")

    def mutating_handler(_event):
        calls.append("mutating")
        isolated_eventmanager.remove_event_listener(
            EventType.ConfigChanged,
            removed_handler,
        )
        isolated_eventmanager.add_event_listener(
            EventType.ConfigChanged,
            late_handler,
        )

    isolated_eventmanager.add_event_listener(
        EventType.ConfigChanged,
        mutating_handler,
    )
    isolated_eventmanager.add_event_listener(
        EventType.ConfigChanged,
        removed_handler,
    )

    dispatch = isolated_eventmanager._EventManager__dispatch_broadcast_event
    dispatch(Event(EventType.ConfigChanged, {}))
    assert calls == ["mutating", "removed"]

    calls.clear()
    dispatch(Event(EventType.ConfigChanged, {}))
    assert calls == ["mutating", "late"]


def test_sync_chain_dispatch_uses_subscription_snapshot(isolated_eventmanager):
    """同步链式事件中的订阅变更不影响当前处理器序列。"""
    calls = []

    def late_handler(_event):
        calls.append("late")

    def removed_handler(_event):
        calls.append("removed")

    def mutating_handler(_event):
        calls.append("mutating")
        isolated_eventmanager.remove_event_listener(
            ChainEventType.NameRecognize,
            removed_handler,
        )
        isolated_eventmanager.add_event_listener(
            ChainEventType.NameRecognize,
            late_handler,
        )

    isolated_eventmanager.add_event_listener(
        ChainEventType.NameRecognize,
        mutating_handler,
    )
    isolated_eventmanager.add_event_listener(
        ChainEventType.NameRecognize,
        removed_handler,
    )

    dispatch = isolated_eventmanager._EventManager__dispatch_chain_event
    assert dispatch(Event(ChainEventType.NameRecognize, {})) is True
    assert calls == ["mutating", "removed"]

    calls.clear()
    assert dispatch(Event(ChainEventType.NameRecognize, {})) is True
    assert calls == ["mutating", "late"]


@pytest.mark.asyncio
async def test_async_chain_dispatch_uses_subscription_snapshot(
        isolated_eventmanager,
):
    """异步链式事件中的订阅变更不影响当前处理器序列。"""
    calls = []

    async def late_handler(_event):
        calls.append("late")

    async def removed_handler(_event):
        calls.append("removed")

    async def mutating_handler(_event):
        calls.append("mutating")
        isolated_eventmanager.remove_event_listener(
            ChainEventType.NameRecognize,
            removed_handler,
        )
        isolated_eventmanager.add_event_listener(
            ChainEventType.NameRecognize,
            late_handler,
        )

    isolated_eventmanager.add_event_listener(
        ChainEventType.NameRecognize,
        mutating_handler,
    )
    isolated_eventmanager.add_event_listener(
        ChainEventType.NameRecognize,
        removed_handler,
    )

    dispatch = isolated_eventmanager._EventManager__dispatch_chain_event_async
    assert await dispatch(Event(ChainEventType.NameRecognize, {})) is True
    assert calls == ["mutating", "removed"]

    calls.clear()
    assert await dispatch(Event(ChainEventType.NameRecognize, {})) is True
    assert calls == ["mutating", "late"]


@pytest.mark.asyncio
async def test_async_broadcast_handles_are_cancelled_on_shutdown(isolated_eventmanager):
    """事件总线关闭时必须取消并收口已投递的异步广播处理器。"""
    global_vars.set_loop(asyncio.get_running_loop())
    handler_count = 5
    active = 0
    cancelled = 0
    all_active = asyncio.Event()

    async def handler(_event):
        nonlocal active, cancelled
        active += 1
        if active == handler_count:
            all_active.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled += 1
            raise

    isolated_eventmanager.add_event_listener(EventType.ConfigChanged, handler)
    isolated_eventmanager.start()
    consumer_threads = tuple(isolated_eventmanager._EventManager__consumer_threads)
    for _ in range(handler_count):
        isolated_eventmanager.send_event(
            EventType.ConfigChanged,
            {"key": {"shutdown"}},
        )

    await asyncio.wait_for(all_active.wait(), timeout=2)
    assert len(isolated_eventmanager._EventManager__async_handles) == handler_count

    await isolated_eventmanager.stop_async()
    await asyncio.sleep(0)

    assert cancelled == handler_count
    assert isolated_eventmanager._EventManager__async_handles == {}
    assert not any(
        thread.is_alive()
        for thread in consumer_threads
    )


@pytest.mark.asyncio
async def test_async_broadcast_shutdown_waits_for_handler_cleanup(
        isolated_eventmanager,
) -> None:
    """提交代理变为 cancelled 后仍须等待处理器 finally 真正完成。"""
    global_vars.set_loop(asyncio.get_running_loop())
    started = asyncio.Event()
    cancelling = asyncio.Event()
    cleanup_release = asyncio.Event()

    async def handler(_event):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelling.set()
            await cleanup_release.wait()
            raise

    isolated_eventmanager.add_event_listener(EventType.ConfigChanged, handler)
    isolated_eventmanager.start()
    isolated_eventmanager.send_event(EventType.ConfigChanged, {"key": {"shutdown"}})
    await asyncio.wait_for(started.wait(), timeout=2)

    stop_task = asyncio.create_task(isolated_eventmanager.stop_async())
    await asyncio.wait_for(cancelling.wait(), timeout=1)
    await asyncio.sleep(0)
    assert not stop_task.done()
    assert isolated_eventmanager._EventManager__lifecycle_state == "stopping"

    stop_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stop_task
    assert isolated_eventmanager._EventManager__async_handles
    assert isolated_eventmanager._EventManager__lifecycle_state == "stopping"

    cleanup_release.set()

    async def wait_until_released() -> None:
        while isolated_eventmanager._EventManager__async_handles:
            await asyncio.sleep(0)

    await asyncio.wait_for(wait_until_released(), timeout=1)
    await isolated_eventmanager.stop_async()
    assert isolated_eventmanager._EventManager__async_handles == {}
    assert isolated_eventmanager._EventManager__lifecycle_state == "stopped"


@pytest.mark.asyncio
async def test_async_broadcast_submission_is_registered_before_stop_snapshot(
        isolated_eventmanager,
        monkeypatch,
) -> None:
    """事件处理器提交和 owner 登记不得被关闭快照从中切开。"""
    global_vars.set_loop(asyncio.get_running_loop())
    submission_entered = threading.Event()
    submission_release = threading.Event()
    real_submit = events_module.asyncio.run_coroutine_threadsafe

    def delayed_submit(coroutine, loop):
        handle = real_submit(coroutine, loop)
        submission_entered.set()
        submission_release.wait(timeout=1)
        return handle

    monkeypatch.setattr(
        events_module.asyncio,
        "run_coroutine_threadsafe",
        delayed_submit,
    )
    isolated_eventmanager.start()

    async def handler() -> None:
        await asyncio.Event().wait()

    submit_thread = threading.Thread(
        target=isolated_eventmanager._EventManager__register_async_handle,
        args=(handler(),),
    )
    submit_thread.start()
    assert await asyncio.to_thread(submission_entered.wait, 1)

    stop_result = []
    stop_thread = threading.Thread(
        target=lambda: stop_result.append(
            isolated_eventmanager._EventManager__begin_stop()
        )
    )
    stop_thread.start()
    await asyncio.sleep(0.02)
    assert stop_thread.is_alive()

    submission_release.set()
    await asyncio.to_thread(submit_thread.join, 1)
    await asyncio.to_thread(stop_thread.join, 1)
    assert not submit_thread.is_alive()
    assert not stop_thread.is_alive()
    assert len(isolated_eventmanager._EventManager__async_handles) == 1

    await isolated_eventmanager.stop_async()
    assert isolated_eventmanager._EventManager__async_handles == {}
