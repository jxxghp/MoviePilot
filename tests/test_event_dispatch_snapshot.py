"""
事件调度订阅快照和生命周期回归测试。
"""

import asyncio
import concurrent.futures
import threading
from queue import PriorityQueue
from unittest.mock import MagicMock

import pytest

from app.runtime import events as events_module
from app.runtime.config import global_vars
from app.runtime.events import Event, eventmanager
from app.schemas.types import ChainEventType, EventType


class _ImmediateExecutor:
    """
    在当前线程执行广播 handler，使订阅变更精确发生在调度迭代期间。
    """

    @staticmethod
    def submit(func, *args, **kwargs):
        """
        立即执行调用并返回符合线程池接口的已完成 Future。
        """
        handle = concurrent.futures.Future()
        try:
            handle.set_result(func(*args, **kwargs))
        except Exception as err:
            handle.set_exception(err)
        return handle


class _DaemonThreadExecutor:
    """
    以守护线程执行回调，让死锁回归测试能够有界失败。
    """

    def __init__(self) -> None:
        """
        初始化可观察的回调完成信号。
        """
        self.finished = threading.Event()

    def submit(self, func, *args, **kwargs):
        """
        在线程中执行调用并返回符合线程池接口的 Future。
        """
        handle = concurrent.futures.Future()

        def run() -> None:
            """
            执行回调并把结果或异常写入 Future。
            """
            if not handle.set_running_or_notify_cancel():
                return
            try:
                handle.set_result(func(*args, **kwargs))
            # 异常交由 Future 保存供调用方消费，此诊断分支不要求覆盖。
            except BaseException as err:  # pragma: no cover
                handle.set_exception(err)
            finally:
                self.finished.set()

        threading.Thread(target=run, daemon=True).start()
        return handle


class _UnboundHandler:
    """
    未创建的宿主 owner，用于验证配置重载的惰性绑定语义。
    """

    async def handle(self, _event):
        """
        不应在 owner 尚未激活时执行。
        """
        raise AssertionError("未激活 owner 不应收到严格配置事件")


@pytest.fixture
def isolated_eventmanager(monkeypatch):
    """
    隔离全局事件总线的订阅表和广播执行器。
    """
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
    """
    广播事件中新增或移除的 handler 从下一个事件开始生效。
    """
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


def test_strict_broadcast_waits_and_propagates_handler_failure(
    isolated_eventmanager,
    monkeypatch,
):
    """
    durable 广播不入队，并在真实 handler 失败时阻止调用方结算。
    """
    isolated_eventmanager._EventManager__lifecycle_state = "running"
    calls = []

    def handler(event):
        """
        记录稳定键后模拟真实 handler 失败。
        """
        calls.append(event.event_data["idempotency_key"])
        raise RuntimeError("delivery failed")

    monkeypatch.setattr(
        isolated_eventmanager,
        "_EventManager__handle_event_error",
        lambda *_args, **_kwargs: None,
    )
    isolated_eventmanager.add_event_listener(EventType.ConfigChanged, handler)

    with pytest.raises(RuntimeError, match="delivery failed"):
        isolated_eventmanager.send_event_strict(
            EventType.ConfigChanged,
            {"idempotency_key": "config.changed:v1"},
        )

    assert calls == ["config.changed:v1"]
    assert isolated_eventmanager._EventManager__event_queue.empty()


@pytest.mark.anyio
async def test_async_strict_broadcast_waits_for_async_handler(isolated_eventmanager):
    """
    异步严格广播必须在调用方返回前等待异步 handler 完成。
    """
    isolated_eventmanager._EventManager__lifecycle_state = "running"
    calls = []

    async def handler(_event):
        """
        模拟需要让出事件循环的配置重载。
        """
        calls.append("start")
        await asyncio.sleep(0)
        calls.append("done")

    isolated_eventmanager.add_event_listener(EventType.ConfigChanged, handler)

    await isolated_eventmanager.async_send_event_strict(
        EventType.ConfigChanged,
        {"key": "AI_AGENT_ENABLE"},
    )

    assert calls == ["start", "done"]
    assert isolated_eventmanager._EventManager__event_queue.empty()


@pytest.mark.anyio
async def test_async_strict_broadcast_skips_unbound_optional_owner(isolated_eventmanager):
    """
    严格配置广播应跳过尚未创建的可选 owner，而不是阻断配置保存。
    """
    isolated_eventmanager._EventManager__lifecycle_state = "running"
    isolated_eventmanager.add_event_listener(
        EventType.ConfigChanged,
        _UnboundHandler.handle,
    )

    await isolated_eventmanager.async_send_event_strict(
        EventType.ConfigChanged,
        {"key": "CACHE_BACKEND_URL"},
    )


def test_sync_chain_dispatch_uses_subscription_snapshot(isolated_eventmanager):
    """
    同步链式事件中的订阅变更不影响当前处理器序列。
    """
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
    """
    异步链式事件中的订阅变更不影响当前处理器序列。
    """
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
    """
    事件总线关闭时必须取消并收口已投递的异步广播处理器。
    """
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
    """
    提交代理变为 cancelled 后仍须等待处理器 finally 真正完成。
    """
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
    """
    同步广播处理器返回前 drain 不得提前宣称结算完成。
    """
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
    """
    drain 超时只报告未收敛，不取消仍在执行的异步业务处理器。
    """
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
    """
    封口 drain 应结算 handler 派生事件，并拒绝封口后的新广播。
    """
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
    """
    stop 开始后 drain 必须报告屏障失败，而非等待残留队列后返回成功。
    """
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


@pytest.mark.parametrize("outcome", ["success", "error", "cancelled"])
def test_sync_completion_removes_owner_before_notifying_waiters(
        isolated_eventmanager,
        monkeypatch,
        outcome,
) -> None:
    """
    同步处理器的完成信号只能在登记清理后发布，关闭等待者不得观察到已完成的残留 owner。

    用真实线程池和闸门固定结算顺序，完成回调同步观察登记表，不依赖线程负载或睡眠时长。

    :param isolated_eventmanager: 订阅和生命周期均隔离的事件总线
    :param monkeypatch: 用例结束时恢复执行器和生命周期属性的夹具
    :param outcome: 真实线程任务正常返回、抛错或尚未运行就取消
    """
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    release_worker = threading.Event()
    worker_started = threading.Event()

    def block_worker() -> None:
        """
        占用唯一执行线程，使待验证任务在观察者登记前保持排队状态。
        """
        worker_started.set()
        release_worker.wait(timeout=5)

    executor.submit(block_worker)
    monkeypatch.setattr(isolated_eventmanager, "_EventManager__executor", executor)
    monkeypatch.setattr(isolated_eventmanager, "_EventManager__lifecycle_state", "running")
    submitted = []
    original_submit = executor.submit

    def record_submit(callback, *args, **kwargs):
        """
        保留真实线程池提交，仅记录原始任务句柄供取消分支使用。

        :param callback: 事件总线包装后的真实同步处理器
        :param args: 线程池原样转发的位置参数
        :param kwargs: 线程池原样转发的关键字参数
        :return: 线程池创建的任务句柄
        """
        handle = original_submit(callback, *args, **kwargs)
        submitted.append(handle)
        return handle

    monkeypatch.setattr(executor, "submit", record_submit)
    observed = []
    notified = threading.Event()

    def handler() -> None:
        """
        在错误分支抛出普通业务异常，其他分支无副作用。
        """
        if outcome == "error":
            raise RuntimeError("handler failed")

    def observe_completion(_completed) -> None:
        """
        在结算通知的同一调用栈观察 owner 表，避免异步断言被后续清理掩盖。
        """
        observed.append(dict(isolated_eventmanager._EventManager__sync_handles))
        notified.set()

    try:
        assert worker_started.wait(timeout=1)
        assert isolated_eventmanager._EventManager__register_sync_handle(handler, ())
        completion = next(iter(isolated_eventmanager._EventManager__sync_handles.values()))
        completion.add_done_callback(observe_completion)
        if outcome == "cancelled":
            assert submitted[0].cancel()
        else:
            release_worker.set()
        assert notified.wait(timeout=1)
        assert observed == [{}]
        assert completion.cancelled() is (outcome == "cancelled")
        if outcome == "error":
            assert isinstance(completion.exception(), RuntimeError)
    finally:
        release_worker.set()
        executor.shutdown(wait=True)
        isolated_eventmanager.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "error", "cancelled"])
async def test_async_stop_waits_for_owner_cleanup_before_completion_callback_is_registered(
        isolated_eventmanager,
        monkeypatch,
        outcome,
) -> None:
    """
    跨线程提交在释放登记锁后暂停时，异步停止也不能留下已经结束的处理器登记。

    暂停点位于原有登记锁释放与完成清理回调登记之间，处理器仍在真实事件循环中执行。

    :param isolated_eventmanager: 订阅和生命周期均隔离的事件总线
    :param monkeypatch: 用例结束时恢复生命周期锁和状态的夹具
    :param outcome: 真实异步处理器正常返回、抛错或取消
    """
    global_vars.set_loop(asyncio.get_running_loop())
    monkeypatch.setattr(isolated_eventmanager, "_EventManager__lifecycle_state", "running")
    original_lock = isolated_eventmanager._EventManager__lifecycle_lock
    registration_paused = threading.Event()
    release_registration = threading.Event()
    handler_release = asyncio.Event()
    handler_finished = asyncio.Event()
    registration_results = []

    async def handler() -> None:
        """
        等待登记线程到达指定间隙后再完成真实处理器。
        """
        await handler_release.wait()
        handler_finished.set()
        if outcome == "error":
            raise RuntimeError("handler failed")
        if outcome == "cancelled":
            raise asyncio.CancelledError()

    def register() -> None:
        """
        在线程中走真实异步处理器提交入口，避免在主事件循环上阻塞闸门。
        """
        registration_results.append(isolated_eventmanager._EventManager__register_async_handle(handler()))

    registration_thread = threading.Thread(target=register, daemon=True)

    def pause_after_registration(*exception_info):
        """
        释放真实锁后仅暂停提交线程，让关闭操作观察回调登记之前的合法线程交错。

        :param exception_info: with语句原样交给真实锁的异常信息
        :return: 真实锁的退出结果，不改变异常传播
        """
        result = original_lock.__exit__(*exception_info)
        if threading.current_thread() is registration_thread and not registration_paused.is_set():
            registration_paused.set()
            release_registration.wait(timeout=5)
        return result

    proxy_lock = MagicMock()
    proxy_lock.__enter__.side_effect = original_lock.__enter__
    proxy_lock.__exit__.side_effect = pause_after_registration
    monkeypatch.setattr(isolated_eventmanager, "_EventManager__lifecycle_lock", proxy_lock)
    registration_thread.start()
    try:
        assert await asyncio.to_thread(registration_paused.wait, 1)
        handler_release.set()
        await asyncio.wait_for(handler_finished.wait(), timeout=1)
        await asyncio.wait_for(isolated_eventmanager.stop_async(), timeout=1)
        assert isolated_eventmanager._EventManager__async_handles == {}
    finally:
        handler_release.set()
        release_registration.set()
        await asyncio.to_thread(registration_thread.join, 1)
        assert not registration_thread.is_alive()
        if isolated_eventmanager._EventManager__lifecycle_state != "stopped":
            await isolated_eventmanager.stop_async()
    assert registration_results == [True]


@pytest.mark.asyncio
async def test_unstarted_async_submission_removes_owner_before_cancel_notification(
        isolated_eventmanager,
        monkeypatch,
) -> None:
    """
    尚未启动的异步提交被取消时，也须先清除登记再通知关闭等待者。

    :param isolated_eventmanager: 订阅和生命周期均隔离的事件总线
    :param monkeypatch: 用例结束时恢复生命周期状态的夹具
    """
    global_vars.set_loop(asyncio.get_running_loop())
    monkeypatch.setattr(isolated_eventmanager, "_EventManager__lifecycle_state", "running")
    observed = []
    called = False

    async def handler() -> None:
        """
        记录不应发生的处理器启动。
        """
        nonlocal called
        called = True

    assert isolated_eventmanager._EventManager__register_async_handle(handler())
    registered = next(iter(isolated_eventmanager._EventManager__async_handles.values()))
    registered.completion.add_done_callback(
        lambda _completed: observed.append(dict(isolated_eventmanager._EventManager__async_handles))
    )
    try:
        assert registered.handle.cancel()
        assert observed == [{}]
        assert registered.completion.cancelled()
    finally:
        await isolated_eventmanager.stop_async()
        # 提交代理的取消回调仍需让事件循环收口其尚未启动的内部包装任务。
        await asyncio.sleep(0)
        await asyncio.sleep(0)
    assert not called


@pytest.mark.asyncio
async def test_repeated_async_stop_balances_stop_sentinels(
        isolated_eventmanager,
) -> None:
    """
    重复异步停止不得遗留哨兵或破坏队列 unfinished_tasks 计数。
    """
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
    """
    同步兼容停止入口也不得遗留仍运行的同步广播处理器。
    """
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
    """
    同步 handler 调用旧 stop() 时不得等待承载自己的 Future。
    """
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
    """
    异步 handler 调用 stop_async() 时不得取消或等待自身 completion。
    """
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
        """
        等待 handler 返回后的 completion 回调移除 owner。
        """
        while isolated_eventmanager._EventManager__async_handles:
            await asyncio.sleep(0)

    await asyncio.wait_for(wait_for_owner_release(), timeout=1)
    assert cancelled is False
    assert isolated_eventmanager._EventManager__lifecycle_state == "stopped"


@pytest.mark.asyncio
async def test_broadcast_handler_cannot_seal_while_own_completion_is_pending(
        isolated_eventmanager,
) -> None:
    """
    handler 内 drain 应立即失败，且不能把仍运行的事件总线伪装成已封口。
    """
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
    """
    事件处理器提交和 owner 登记不得被关闭快照从中切开。
    """
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


@pytest.mark.parametrize("event_type", [EventType.TransferFailed, EventType.SubtitleTransferFailed,
                                        EventType.AudioTransferFailed])
def test_outbox_retries_strict_failure_without_repeating_system_alert(
        isolated_eventmanager, monkeypatch, event_type):
    """
    真实失败 handler 仍重试至死信，相同事件的系统错误提示只发送一次。
    """
    from unittest.mock import Mock

    from app.application.outbox import ClaimedOutboxMessage, OutboxDispatcher
    from app.runtime.event.errors import EventErrorPolicy
    from app.startup.composition import outbox

    isolated_eventmanager._EventManager__lifecycle_state = "running"
    notify, emit = Mock(), Mock()
    monkeypatch.setattr(isolated_eventmanager, "_EventManager__error_policy",
                        EventErrorPolicy(notifier=lambda: notify, emit_system_error=emit))
    monkeypatch.setattr(outbox, "EventManager", lambda: isolated_eventmanager)
    attempts = []

    def failed_handler(event):
        """
        模拟稳定重现的插件处理器故障。
        """
        attempts.append(event.event_data["idempotency_key"])
        raise RuntimeError("broken transfer consumer")

    isolated_eventmanager.add_event_listener(event_type, failed_handler)
    store = Mock()
    event_key = f"{event_type.value}:task-1:1"
    store.claim.side_effect = [
        ClaimedOutboxMessage(1, event_key, event_type.value,
                             {"idempotency_key": event_key,
                              "transferinfo": {"success": False, "message": "failed"}}, 1, attempt)
        for attempt in range(1, 6)
    ] + [None]
    dispatcher = OutboxDispatcher(store, outbox.build_outbox_handlers())
    for _ in range(5):
        assert dispatcher.dispatch_one()
    assert not dispatcher.dispatch_one()
    assert attempts == [event_key] * 5
    assert [call.kwargs["dead"] for call in store.retry.call_args_list] == [False] * 4 + [True]
    store.complete.assert_not_called()
    notify.assert_called_once()
    emit.assert_called_once()
