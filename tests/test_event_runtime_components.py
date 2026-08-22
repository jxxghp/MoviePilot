"""事件注册、绑定、调度和错误策略组件的独立测试。"""

import sys
import threading
import types
from unittest.mock import Mock

from app.runtime.event.binding import (
    EventBindingResolver,
    EventHandlerBinding,
)
from app.runtime.event.errors import EventErrorPolicy
from app.runtime.events import Event
from app.schemas.types import EventType
from app.startup.modules_initializer import get_host_event_handler_factories


class _UnmanagedHandler:
    """记录构造次数，用于证明绑定未命中时不会被总线实例化。"""

    constructed = 0

    def __init__(self) -> None:
        """记录任何非预期的隐式构造。"""
        type(self).constructed += 1

    def handle(self, _event: Event) -> None:
        """提供可解析的实例方法声明。"""


def _free_function_handler(_event: Event) -> None:
    """模块级自由函数处理器，用于验证直调路径保持不变。"""


def test_binding_miss_does_not_construct_handler_owner() -> None:
    """resolver 未命中时只记录诊断，不能调用 owner_class()。"""
    resolvers = {}
    binding = EventBindingResolver(
        lock=threading.Lock(),
        resolvers=lambda: resolvers,
    )
    _UnmanagedHandler.constructed = 0

    assert binding.resolve(_UnmanagedHandler.handle) == []
    assert _UnmanagedHandler.constructed == 0
    assert binding.unresolved_handlers() == (
        f"{__name__}._UnmanagedHandler.handle",
    )


def test_unloaded_module_class_handler_is_skipped() -> None:
    """模块缓存被清除后，残留的类方法声明必须跳过而非直调原始函数。"""
    fake_name = "tests._fake_unloaded_plugin"
    fake_module = types.ModuleType(fake_name)
    sys.modules[fake_name] = fake_module
    try:
        # 在伪模块命名空间内构造类，使处理器 __module__ 指向该模块
        exec(
            "class _ResidualPlugin:\n"
            "    def reload(self, event):\n"
            "        raise AssertionError('residual handler must not run')\n",
            fake_module.__dict__,
        )
        residual_handler = fake_module._ResidualPlugin.reload
        # 模拟插件重载 stop 阶段清除模块缓存后的残留注册
        del sys.modules[fake_name]

        binding = EventBindingResolver(
            lock=threading.Lock(),
            resolvers=lambda: {},
        )
        assert binding.resolve(residual_handler) is None
        # 模块卸载后 identifier 回退为 unknown_module 前缀，与线上日志一致
        assert binding.unresolved_handlers() == (
            "unknown_module._ResidualPlugin.reload",
        )
    finally:
        sys.modules.pop(fake_name, None)


def test_unloaded_module_decorator_wrapped_method_is_skipped() -> None:
    """装饰器包装的类方法限定名含 <locals>，模块卸载后也必须跳过而非直调。"""
    fake_name = "tests._fake_unloaded_decorated_plugin"
    fake_module = types.ModuleType(fake_name)
    sys.modules[fake_name] = fake_module
    try:
        exec(
            "def _deco(f):\n"
            "    def wrapper(self, event):\n"
            "        return f(self, event)\n"
            "    return wrapper\n"
            "class _DecoratedPlugin:\n"
            "    @_deco\n"
            "    def send_msg(self, event):\n"
            "        raise AssertionError('residual handler must not run')\n",
            fake_module.__dict__,
        )
        residual_handler = fake_module._DecoratedPlugin.send_msg
        # 装饰器包装后限定名含 <locals>，不能因此被误判为自由函数
        assert "<locals>" in residual_handler.__qualname__
        del sys.modules[fake_name]

        binding = EventBindingResolver(
            lock=threading.Lock(),
            resolvers=lambda: {},
        )
        assert binding.resolve(residual_handler) is None
        assert binding.unresolved_handlers() == (
            "unknown_module._deco.<locals>.wrapper",
        )
    finally:
        sys.modules.pop(fake_name, None)


def test_free_function_handler_still_invoked_directly() -> None:
    """自由函数处理器不属于类声明，保持直调路径不被新跳过逻辑影响。"""
    binding = EventBindingResolver(
        lock=threading.Lock(),
        resolvers=lambda: {},
    )

    resolved = binding.resolve(_free_function_handler)

    assert resolved is not None
    method, handler_binding, class_name, method_name = resolved
    assert method is _free_function_handler
    assert handler_binding.run_sync_in_threadpool is True
    assert class_name == ""
    assert method_name == "_free_function_handler"


def test_binding_uses_explicit_resolver_instance() -> None:
    """显式 resolver 应返回当前托管实例上的绑定方法。"""
    instance = object.__new__(_UnmanagedHandler)
    resolvers = {
        "test": lambda owner: [
            EventHandlerBinding(
                instance=instance,
                owner_name="托管处理器",
            )
        ]
        if owner is _UnmanagedHandler
        else None
    }
    binding = EventBindingResolver(
        lock=threading.Lock(),
        resolvers=lambda: resolvers,
    )

    [(method, resolved, class_name, method_name)] = binding.resolve(
        _UnmanagedHandler.handle
    )

    assert method.__self__ is instance
    assert resolved.owner_name == "托管处理器"
    assert class_name == "_UnmanagedHandler"
    assert method_name == "handle"


def test_system_error_failure_does_not_rebroadcast() -> None:
    """SystemError 处理器自身失败时只能通知和日志降级，不能再次发送事件。"""
    notifier = Mock()
    emit = Mock()
    policy = EventErrorPolicy(
        notifier=lambda: notifier,
        emit_system_error=emit,
    )

    policy.handle(
        event=Event(EventType.SystemError, {}),
        module_name="测试模块",
        class_name="BrokenHandler",
        method_name="handle",
        error=RuntimeError("broken"),
    )

    notifier.assert_called_once()
    emit.assert_not_called()


def test_regular_event_failure_emits_one_system_error() -> None:
    """普通事件失败应生成一次结构稳定的 SystemError 载荷。"""
    emit = Mock()
    policy = EventErrorPolicy(
        notifier=lambda: None,
        emit_system_error=emit,
    )

    policy.handle(
        event=Event(EventType.ConfigChanged, {}),
        module_name="测试模块",
        class_name="BrokenHandler",
        method_name="handle",
        error=RuntimeError("broken"),
    )

    payload = emit.call_args.args[0]
    assert payload["type"] == "event"
    assert payload["event_type"] is EventType.ConfigChanged
    assert payload["event_handle"] == "BrokenHandler.handle"
    assert payload["error"] == "broken"


def test_all_decorated_host_handler_classes_have_explicit_factories() -> None:
    """宿主中使用事件装饰器的类必须全部由组合根 resolver 白名单接管。"""
    factories = get_host_event_handler_factories()

    assert {owner.__name__ for owner in factories} == {
        "Command",
        "DownloadChain",
        # 插件重载处理器声明在混入类上，其实例由调度器组合根提供
        "PluginScheduling",
        "Scheduler",
        "ScrapingChain",
        "SearchChain",
        "SiteChain",
        "SubscribeChain",
        "WorkflowChain",
    }
