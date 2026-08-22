"""事件分发调用处理器方法时绑定归属插件实例日志上下文的行为契约测试。

`EventDispatcher.invoke_sync`/`invoke_async` 是宿主自己控制的插件方法调用点之一
（另两处是插件定时服务见 test_plugin_multi_instance_scheduler.py、插件初始化）；
这里直接构造 EventDispatcher 并注入最小依赖，验证处理器方法执行期间能读到正确的
(插件标识, 实例标识)，执行完毕或抛异常后都不残留绑定。
"""

import asyncio
from types import SimpleNamespace

import pytest

from app.runtime import log as log_module
from app.runtime.event.binding import EventHandlerBinding
from app.runtime.event.dispatch import EventDispatcher
from app.schemas.types import EventType


class _FakeBindingResolver:
    """返回固定绑定列表的解析器替身。"""

    def __init__(self, resolved):
        self._resolved = resolved

    def resolve(self, _handler):
        return self._resolved


def _build_dispatcher(resolved, error_handler=None):
    """构造只注入 invoke_sync/invoke_async 所需依赖的 EventDispatcher。"""
    errors = []
    return EventDispatcher(
        registry=None,
        binding_resolver=_FakeBindingResolver(resolved),
        executor=lambda: None,
        event_loop=lambda: None,
        event_factory=lambda **kwargs: None,
        error_handler=error_handler or (lambda **kwargs: errors.append(kwargs)),
    ), errors


def _stub_event() -> SimpleNamespace:
    """最小可用的事件替身。

    `invoke_sync`/`invoke_async` 现在还会读取 `correlation_id`（关联 ID 传播）与
    `event_type`（低基数耗时观测的标签），这两项与本文件要盯的实例日志上下文绑定
    无关，但两个方法体都会先访问它们，裸 `object()` 会在到达被测行为之前就
    AttributeError。这里给一个只补齐这两个字段的替身，不引入真实 Event 模型的
    校验与其它字段。
    """
    return SimpleNamespace(correlation_id=None, event_type=EventType.PluginAction)


def test_invoke_sync_binds_plugin_instance_from_binding_instance_key():
    """同步调用时按 `EventHandlerBinding.instance_key` 绑定归属实例的日志上下文。"""
    observed = {}

    def handler(_event):
        observed["ctx"] = log_module.LoggerManager._resolve_plugin_instance(None)

    binding = EventHandlerBinding(instance=object(), owner_name="DemoPlugin@second", instance_key="DemoPlugin@second")
    dispatcher, _errors = _build_dispatcher([(handler, binding, "DemoPlugin", "on_event")])

    dispatcher.invoke_sync(handler, event=_stub_event())

    assert observed["ctx"] == ("DemoPlugin", "second")
    # 调用结束后绑定应已复原
    assert log_module.LoggerManager._resolve_plugin_instance(None) == (None, None)


def test_invoke_sync_without_instance_key_does_not_bind_context():
    """未登记实例解析器的处理器（`instance_key` 为空）不绑定插件日志上下文。"""
    observed = {}

    def handler(_event):
        observed["ctx"] = log_module.LoggerManager._resolve_plugin_instance(None)

    binding = EventHandlerBinding(instance=object(), owner_name="free_function")
    dispatcher, _errors = _build_dispatcher([(handler, binding, "", "on_event")])

    dispatcher.invoke_sync(handler, event=_stub_event())

    assert observed["ctx"] == (None, None)


def test_invoke_sync_resets_binding_even_when_handler_raises():
    """处理器抛异常时绑定仍需正确复原，交由外层错误处理策略上报。"""
    binding = EventHandlerBinding(instance=object(), owner_name="DemoPlugin", instance_key="DemoPlugin")

    def failing_handler(_event):
        raise RuntimeError("boom")

    dispatcher, errors = _build_dispatcher([(failing_handler, binding, "DemoPlugin", "on_event")])

    dispatcher.invoke_sync(failing_handler, event=_stub_event())

    assert len(errors) == 1
    assert log_module.LoggerManager._resolve_plugin_instance(None) == (None, None)


@pytest.mark.asyncio
async def test_invoke_async_binds_plugin_instance_for_coroutine_handler():
    """异步协程处理器执行期间同样能读到绑定的实例上下文。"""
    observed = {}

    async def handler(_event):
        observed["ctx"] = log_module.LoggerManager._resolve_plugin_instance(None)

    binding = EventHandlerBinding(instance=object(), owner_name="DemoPlugin@second", instance_key="DemoPlugin@second")
    dispatcher, _errors = _build_dispatcher([(handler, binding, "DemoPlugin", "on_event")])

    await dispatcher.invoke_async(handler, event=_stub_event())

    assert observed["ctx"] == ("DemoPlugin", "second")
    assert log_module.LoggerManager._resolve_plugin_instance(None) == (None, None)


@pytest.mark.asyncio
async def test_invoke_async_binds_plugin_instance_for_threadpool_handler():
    """在线程池执行的同步处理器（`run_sync_in_threadpool=True`）也能读到绑定。"""
    observed = {}

    def handler(_event):
        observed["ctx"] = log_module.LoggerManager._resolve_plugin_instance(None)

    binding = EventHandlerBinding(
        instance=object(),
        owner_name="DemoPlugin@second",
        run_sync_in_threadpool=True,
        instance_key="DemoPlugin@second",
    )
    dispatcher, _errors = _build_dispatcher([(handler, binding, "DemoPlugin", "on_event")])

    await dispatcher.invoke_async(handler, event=_stub_event())

    assert observed["ctx"] == ("DemoPlugin", "second")
