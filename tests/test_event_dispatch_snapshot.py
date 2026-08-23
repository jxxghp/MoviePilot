"""事件调度订阅快照和生命周期回归测试。"""

import asyncio
import concurrent.futures
import threading
from queue import PriorityQueue

import pytest

from app.runtime.config import global_vars
from app.runtime import events as events_module
from app.runtime.events import Event, eventmanager
from app.schemas.types import ChainEventType, EventType


class _ImmediateExecutor:
    """在当前线程执行广播 handler，使订阅变更精确发生在调度迭代期间。"""

    @staticmethod
    def submit(func, *args, **kwargs):
        """立即执行调用并返回符合线程池接口的已完成 Future。"""
        handle = concurrent.futures.Future()
        try:
            handle.set_result(func(*args, **kwargs))
        except Exception as err:
            handle.set_exception(err)
        return handle


class _DaemonThreadExecutor:
    """以守护线程执行回调，让死锁回归测试能够有界失败。"""

    def __init__(self) -> None:
        """初始化可观察的回调完成信号。"""
        self.finished = threading.Event()

    def submit(self, func, *args, **kwargs):
        """在线程中执行调用并返回符合线程池接口的 Future。"""
        handle = concurrent.futures.Future()

        def run() -> None:
            """执行回调并把结果或异常写入 Future。"""
            if not handle.set_running_or_notify_cancel():
                return
            try:
                handle.set_result(func(*args, **kwargs))
            except BaseException as err:  # pragma: no cover - 交由 Future 消费
                handle.set_exception(err)
            finally:
                self.finished.set()

        threading.Thread(target=run, daemon=True).start()
        return handle


@pytest.fixture
def isolated_eventmanager(monkeypatch):
    """隔离全局事件总线的订阅表和广播执行器。"""
    monkeypatch.setattr(
        global_vars,
        "CURRENT_EVENT_LOOP",
        global_vars.CURRENT_EVENT_LOOP,
    )
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
        "_EventManager__disabled_handlers",
        set(),
    )
    monkeypatch.setattr(
        eventmanager,
        "_EventManager__disabled_classes",
        set(),
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
        "_EventManager__event_queue",
        PriorityQueue(),
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
        "_EventManager__sync_handles",
        {},
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
    isolated_eventmanager._EventManager__lifecycle_state = "running"

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
async def test_drain_waits_for_slow_sync_broadcast_handler(
        isolated_eventmanager,
        monkeypatch,
) -> None:
    """同步广播处理器返回前 drain 不得提前宣称结算完成。"""
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    monkeypatch.setattr(
        isolated_eventmanager,
        "_EventManager__executor",
        executor,
    )
    started = threading.Event()
    release = threading.Event()

    def handler(_event) -> None:
        started.set()
        release.wait(timeout=2)

    isolated_eventmanager.add_event_listener(EventType.ConfigChanged, handler)
    isolated_eventmanager.start()
    try:
        isolated_eventmanager.send_event(
            EventType.ConfigChanged,
            {"key": {"drain-sync"}},
        )
        assert await asyncio.to_thread(started.wait, 1)
        assert len(isolated_eventmanager._EventManager__sync_handles) == 1

        drain_task = asyncio.create_task(
            isolated_eventmanager.drain_async(timeout=1)
        )
        await asyncio.sleep(0.02)
        assert not drain_task.done()

        release.set()
        assert await drain_task is True
        assert isolated_eventmanager._EventManager__sync_handles == {}
        assert (
            isolated_eventmanager._EventManager__event_queue.unfinished_tasks
            == 0
        )
    finally:
        release.set()
        await isolated_eventmanager.stop_async()
        executor.shutdown(wait=True)


@pytest.mark.asyncio
async def test_drain_timeout_does_not_cancel_async_broadcast_handler(
        isolated_eventmanager,
) -> None:
    """drain 超时只报告未收敛，不取消仍在执行的异步业务处理器。"""
    global_vars.set_loop(asyncio.get_running_loop())
    started = asyncio.Event()
    release = asyncio.Event()
    cancelled = False

    async def handler(_event) -> None:
        nonlocal cancelled
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled = True
            raise

    isolated_eventmanager.add_event_listener(EventType.ConfigChanged, handler)
    isolated_eventmanager.start()
    try:
        isolated_eventmanager.send_event(
            EventType.ConfigChanged,
            {"key": {"drain-async"}},
        )
        await asyncio.wait_for(started.wait(), timeout=1)

        assert await isolated_eventmanager.drain_async(timeout=0.01) is False
        assert cancelled is False
        assert isolated_eventmanager._EventManager__async_handles

        release.set()
        assert await isolated_eventmanager.drain_async(timeout=1) is True
        assert cancelled is False
    finally:
        release.set()
        await isolated_eventmanager.stop_async()


@pytest.mark.asyncio
async def test_sealed_drain_waits_for_cascade_and_rejects_later_broadcasts(
        isolated_eventmanager,
) -> None:
    """封口 drain 应结算 handler 派生事件，并拒绝封口后的新广播。"""
    calls = []

    def first_handler(_event) -> None:
        calls.append("first")
        isolated_eventmanager.send_event(EventType.ModuleReload, {})

    def cascaded_handler(_event) -> None:
        calls.append("cascaded")

    isolated_eventmanager.add_event_listener(
        EventType.ConfigChanged,
        first_handler,
    )
    isolated_eventmanager.add_event_listener(
        EventType.ModuleReload,
        cascaded_handler,
    )
    isolated_eventmanager.start()
    try:
        isolated_eventmanager.send_event(
            EventType.ConfigChanged,
            {"key": {"cascade"}},
        )
        assert await isolated_eventmanager.drain_async(
            timeout=1,
            seal=True,
        ) is True
        assert calls == ["first", "cascaded"]
        queue_size = isolated_eventmanager._EventManager__event_queue.qsize()

        isolated_eventmanager.send_event(EventType.ModuleReload, {})
        await asyncio.sleep(0.02)

        assert calls == ["first", "cascaded"]
        assert (
            isolated_eventmanager._EventManager__event_queue.qsize()
            == queue_size
        )
        assert isolated_eventmanager._EventManager__lifecycle_state == "sealed"
    finally:
        await isolated_eventmanager.stop_async()


@pytest.mark.asyncio
async def test_drain_returns_false_when_stop_interleaves(
        isolated_eventmanager,
        monkeypatch,
) -> None:
    """stop 开始后 drain 必须报告屏障失败，而非等待残留队列后返回成功。"""
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    monkeypatch.setattr(
        isolated_eventmanager,
        "_EventManager__executor",
        executor,
    )
    started = threading.Event()
    release = threading.Event()

    def handler(_event) -> None:
        started.set()
        release.wait(timeout=2)

    isolated_eventmanager.add_event_listener(EventType.ConfigChanged, handler)
    isolated_eventmanager.start()
    try:
        isolated_eventmanager.send_event(
            EventType.ConfigChanged,
            {"key": {"stop-race"}},
        )
        assert await asyncio.to_thread(started.wait, 1)
        drain_task = asyncio.create_task(isolated_eventmanager.drain_async())
        stop_task = asyncio.create_task(isolated_eventmanager.stop_async())

        assert await asyncio.wait_for(drain_task, timeout=1) is False
        assert not stop_task.done()

        release.set()
        await asyncio.wait_for(stop_task, timeout=1)
        assert isolated_eventmanager._EventManager__sync_handles == {}
    finally:
        release.set()
        if isolated_eventmanager._EventManager__lifecycle_state != "stopped":
            await isolated_eventmanager.stop_async()
        executor.shutdown(wait=True)


@pytest.mark.asyncio
async def test_repeated_async_stop_balances_stop_sentinels(
        isolated_eventmanager,
) -> None:
    """重复异步停止不得遗留哨兵或破坏队列 unfinished_tasks 计数。"""
    isolated_eventmanager.start()

    await asyncio.gather(
        isolated_eventmanager.stop_async(),
        isolated_eventmanager.stop_async(),
    )
    await isolated_eventmanager.stop_async()

    assert isolated_eventmanager._EventManager__event_queue.qsize() == 0
    assert isolated_eventmanager._EventManager__event_queue.unfinished_tasks == 0


@pytest.mark.asyncio
async def test_sync_stop_waits_for_owned_sync_handler(
        isolated_eventmanager,
        monkeypatch,
) -> None:
    """同步兼容停止入口也不得遗留仍运行的同步广播处理器。"""
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    monkeypatch.setattr(
        isolated_eventmanager,
        "_EventManager__executor",
        executor,
    )
    started = threading.Event()
    release = threading.Event()

    def handler(_event) -> None:
        started.set()
        release.wait(timeout=2)

    isolated_eventmanager.add_event_listener(EventType.ConfigChanged, handler)
    isolated_eventmanager.start()
    try:
        isolated_eventmanager.send_event(
            EventType.ConfigChanged,
            {"key": {"sync-stop"}},
        )
        assert await asyncio.to_thread(started.wait, 1)
        stop_task = asyncio.create_task(
            asyncio.to_thread(isolated_eventmanager.stop)
        )
        await asyncio.sleep(0.02)
        assert not stop_task.done()

        release.set()
        await asyncio.wait_for(stop_task, timeout=1)
        assert isolated_eventmanager._EventManager__sync_handles == {}
        assert (
            isolated_eventmanager._EventManager__event_queue.unfinished_tasks
            == 0
        )
    finally:
        release.set()
        if isolated_eventmanager._EventManager__lifecycle_state != "stopped":
            await isolated_eventmanager.stop_async()
        executor.shutdown(wait=True)


def test_sync_broadcast_handler_can_call_legacy_stop_without_self_wait(
        isolated_eventmanager,
        monkeypatch,
) -> None:
    """同步 handler 调用旧 stop() 时不得等待承载自己的 Future。"""
    executor = _DaemonThreadExecutor()
    monkeypatch.setattr(
        isolated_eventmanager,
        "_EventManager__executor",
        executor,
    )
    stopped = threading.Event()

    def handler(_event) -> None:
        isolated_eventmanager.stop()
        stopped.set()

    isolated_eventmanager.add_event_listener(EventType.ConfigChanged, handler)
    isolated_eventmanager.start()
    try:
        isolated_eventmanager.send_event(
            EventType.ConfigChanged,
            {"key": {"handler-stop"}},
        )

        assert stopped.wait(timeout=1)
        assert executor.finished.wait(timeout=1)
        assert isolated_eventmanager._EventManager__sync_handles == {}
        assert isolated_eventmanager._EventManager__lifecycle_state == "stopped"
    finally:
        if isolated_eventmanager._EventManager__lifecycle_state == "running":
            consumer_threads = isolated_eventmanager._EventManager__begin_stop()
            for consumer_thread in consumer_threads:
                consumer_thread.join(timeout=1)
            isolated_eventmanager._EventManager__discard_stop_sentinels()


@pytest.mark.asyncio
async def test_async_broadcast_handler_can_await_stop_without_self_cancel(
        isolated_eventmanager,
) -> None:
    """异步 handler 调用 stop_async() 时不得取消或等待自身 completion。"""
    global_vars.set_loop(asyncio.get_running_loop())
    stopped = asyncio.Event()
    cancelled = False

    async def handler(_event) -> None:
        nonlocal cancelled
        try:
            await isolated_eventmanager.stop_async()
            stopped.set()
        except asyncio.CancelledError:
            cancelled = True
            raise

    isolated_eventmanager.add_event_listener(EventType.ConfigChanged, handler)
    isolated_eventmanager.start()
    isolated_eventmanager.send_event(
        EventType.ConfigChanged,
        {"key": {"async-handler-stop"}},
    )

    await asyncio.wait_for(stopped.wait(), timeout=1)

    async def wait_for_owner_release() -> None:
        """等待 handler 返回后的 completion 回调移除 owner。"""
        while isolated_eventmanager._EventManager__async_handles:
            await asyncio.sleep(0)

    await asyncio.wait_for(wait_for_owner_release(), timeout=1)
    assert cancelled is False
    assert isolated_eventmanager._EventManager__lifecycle_state == "stopped"


@pytest.mark.asyncio
async def test_broadcast_handler_cannot_seal_while_own_completion_is_pending(
        isolated_eventmanager,
) -> None:
    """handler 内 drain 应立即失败，且不能把仍运行的事件总线伪装成已封口。"""
    global_vars.set_loop(asyncio.get_running_loop())
    drain_results = []
    handler_done = asyncio.Event()

    async def handler(_event) -> None:
        drain_results.append(
            await isolated_eventmanager.drain_async(seal=True)
        )
        handler_done.set()

    isolated_eventmanager.add_event_listener(EventType.ConfigChanged, handler)
    isolated_eventmanager.start()
    try:
        isolated_eventmanager.send_event(
            EventType.ConfigChanged,
            {"key": {"handler-drain"}},
        )
        await asyncio.wait_for(handler_done.wait(), timeout=1)

        assert drain_results == [False]
        assert isolated_eventmanager._EventManager__lifecycle_state == "running"
        assert await isolated_eventmanager.drain_async(timeout=1) is True
    finally:
        await isolated_eventmanager.stop_async()


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
