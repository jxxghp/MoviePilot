"""
防抖器参数转发、函数绑定与同步异步选择的兼容契约。
"""

import asyncio
import inspect

import pytest

import app.runtime.debounce as debounce_module
from app.runtime.debounce import AsyncDebouncer, Debouncer, debounce


class _ManualTimer:
    """
    由测试显式推进的定时器，避免线程与墙钟时间影响断言。
    """

    def __init__(self, interval, callback):
        """
        保存真实引擎提供的间隔与回调。
        """
        self.interval = interval
        self.callback = callback
        self.active = False

    def start(self):
        """
        标记为待执行。
        """
        self.active = True

    def cancel(self):
        """
        撤销尚未执行的回调。
        """
        self.active = False

    def is_alive(self):
        """
        提供与线程定时器一致的存活查询。
        """
        return self.active

    def fire(self):
        """
        仅执行仍有效的回调。
        """
        if self.active:
            self.active = False
            self.callback()


@pytest.fixture
def manual_timers(monkeypatch):
    """
    捕获引擎创建的定时器并禁止启动真实线程。
    """
    timers = []

    def create_timer(interval, callback):
        """
        返回可由测试控制的独立定时器。
        """
        timer = _ManualTimer(interval, callback)
        timers.append(timer)
        return timer

    monkeypatch.setattr(debounce_module, "Timer", create_timer)
    return timers


@pytest.mark.parametrize("keyword_constructor", [False, True])
@pytest.mark.parametrize("leading", [False, True])
def test_sync_debouncer_preserves_arguments_and_cooling(
    manual_timers, keyword_constructor, leading
):
    """
    两种构造方式均保留参数，并分别执行首个调用或最后一个调用。
    """
    calls = []

    def record(value, *, label):
        """
        保留位置参数、关键字参数和对象身份。
        """
        calls.append((value, label))
        return "discarded"

    options = {"leading": leading, "enable_logging": False, "source": "test"}
    engine = (
        Debouncer(func=record, interval=7, **options)
        if keyword_constructor
        else Debouncer(record, 7, **options)
    )
    first, last = object(), object()
    assert engine(first, label="first") is None
    assert engine(last, label="last") is None
    assert manual_timers[0].is_alive() is False
    assert manual_timers[-1].interval == 7
    assert calls == ([(first, "first")] if leading else [])
    for timer in manual_timers:
        timer.fire()
    assert calls == ([(first, "first")] if leading else [(last, "last")])
    assert engine.is_cooling_down is False
    engine(first, label="cancelled")
    before_cancel = list(calls)
    assert engine.cancel() is None
    manual_timers[-1].fire()
    assert calls == before_cancel
    assert engine.timer is None


def test_decorator_preserves_method_binding_and_metadata(manual_timers):
    """
    普通函数描述符仍绑定实例，cancel 仍操作原先共享的防抖引擎。
    """
    calls = []

    class Receiver:
        """
        通过实例方法暴露防抖包装器。
        """

        @debounce(3)
        def accept(self, value, *, label):
            """
            记录接收实例与完整调用参数。
            """
            calls.append((self, value, label))

    receiver = Receiver()
    assert receiver.accept("value", label="label") is None
    assert Receiver.accept.__name__ == "accept"
    assert inspect.iscoroutinefunction(receiver.accept) is False
    manual_timers[-1].fire()
    assert calls == [(receiver, "value", "label")]
    receiver.accept("cancelled", label="label")
    assert receiver.accept.cancel() is None
    manual_timers[-1].fire()
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("leading", [False, True])
async def test_async_debouncer_preserves_arguments_and_result_discard(leading):
    """
    协程函数通过异步引擎执行，调用和取消均保持返回 None 的约定。
    """
    calls = []

    async def record(value, *, label):
        """
        记录实际被执行的调用。
        """
        calls.append((value, label))
        return "discarded"

    engine = AsyncDebouncer(func=record, interval=0, leading=leading, source="test")
    assert await engine("first", label=1) is None
    assert await engine("last", label=2) is None
    await asyncio.wait_for(engine.task, timeout=1)
    assert calls == ([("first", 1)] if leading else [("last", 2)])
    assert await engine.cancel() is None
    assert engine.task is None
    assert engine._retired_tasks == set()


@pytest.mark.asyncio
async def test_async_decorator_preserves_binding_and_cancel():
    """
    异步方法包装保留 await、实例绑定、元数据和异步取消接口。
    """
    calls = []

    class Receiver:
        """
        提供异步防抖方法。
        """

        @debounce(60, leading=True)
        async def accept(self, value, *, label):
            """
            记录绑定后的调用并返回被包装器丢弃的结果。
            """
            calls.append((self, value, label))
            return "discarded"

    receiver = Receiver()
    try:
        assert inspect.iscoroutinefunction(receiver.accept)
        assert receiver.accept.__name__ == "accept"
        assert await receiver.accept("value", label="label") is None
        assert calls == [(receiver, "value", "label")]
    finally:
        assert await receiver.accept.cancel() is None


def test_sync_function_returning_awaitable_stays_sync(manual_timers):
    """
    返回可等待对象的普通函数仍走同步引擎，不能仅凭返回类型改选异步引擎。
    """
    events = []

    class Result:
        """
        记录是否有人等待了普通函数的返回值。
        """

        def __await__(self):
            """
            提供不会实际等待的 await 协议。
            """
            events.append("awaited")
            yield from ()
            return "result"

    @debounce(3, leading=True)
    def callback(value):
        """
        返回可等待对象但保持普通函数定义。
        """
        events.append(value)
        return Result()

    assert inspect.iscoroutinefunction(callback) is False
    assert callback("called") is None
    assert events == ["called"]
    assert callback.cancel() is None
    assert manual_timers[-1].is_alive() is False
