"""事件注册、绑定、调度和错误策略组件的独立测试。"""

import threading
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


def test_binding_miss_does_not_construct_handler_owner() -> None:
    """resolver 未命中时只记录诊断，不能调用 owner_class()。"""
    resolvers = {}
    binding = EventBindingResolver(
        lock=threading.Lock(),
        resolvers=lambda: resolvers,
    )
    _UnmanagedHandler.constructed = 0

    assert binding.resolve(_UnmanagedHandler.handle) is None
    assert _UnmanagedHandler.constructed == 0
    assert binding.unresolved_handlers() == (
        f"{__name__}._UnmanagedHandler.handle",
    )


def test_binding_uses_explicit_resolver_instance() -> None:
    """显式 resolver 应返回当前托管实例上的绑定方法。"""
    instance = object.__new__(_UnmanagedHandler)
    resolvers = {
        "test": lambda owner: EventHandlerBinding(
            instance=instance,
            owner_name="托管处理器",
        )
        if owner is _UnmanagedHandler
        else None
    }
    binding = EventBindingResolver(
        lock=threading.Lock(),
        resolvers=lambda: resolvers,
    )

    method, resolved, class_name, method_name = binding.resolve(
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
        "Scheduler",
        "ScrapingChain",
        "SearchChain",
        "SiteChain",
        "SubscribeChain",
        "WorkflowChain",
    }
