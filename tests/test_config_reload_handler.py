"""ConfigReloadMixin 配置变更事件处理器解析的回归测试（Issue #6329）。

ConfigReloadMixin 动态生成的事件处理器，必须能被事件总线解析回实例方法，
不能因 __name__ 与类上方法名不一致而被静默跳过。
"""

import inspect

import pytest

from app.runtime.events import Event, EventHandlerBinding, eventmanager
from app.runtime.reload import ConfigReloadMixin
from app.schemas import ConfigChangeEventData
from app.schemas.types import EventType


class _ReloadRecorder(ConfigReloadMixin):
    """测试用同步重载子类，记录重载调用次数。"""

    CONFIG_WATCH = {"TEST_RELOAD_KEY"}

    def __init__(self):
        self.reload_count = 0

    def on_config_changed(self):
        self.reload_count += 1


class _AsyncReloadRecorder(ConfigReloadMixin):
    """测试用异步重载子类，记录重载调用次数。"""

    CONFIG_WATCH = {"TEST_RELOAD_KEY"}

    def __init__(self):
        self.reload_count = 0

    async def on_config_changed(self):
        self.reload_count += 1


@pytest.fixture(params=[_ReloadRecorder, _AsyncReloadRecorder])
def recorder(request, monkeypatch):
    """将生成的处理器绑定到测试实例，并在测试后移除全局监听。"""
    recorder_cls = request.param
    instance = recorder_cls()

    def resolver(owner_class):
        if owner_class is recorder_cls:
            return [EventHandlerBinding(instance=instance, owner_name=owner_class.__name__)]
        return None

    # 隔离真实解析器，仅绑定测试实例
    monkeypatch.setattr(
        eventmanager,
        "_EventManager__handler_instance_resolvers",
        {"test_reload": resolver},
    )
    yield instance
    # 移除类定义时注册的全局监听，避免影响其余测试
    eventmanager.remove_event_listener(
        EventType.ConfigChanged, recorder_cls.handle_config_changed
    )


def _build_event(keys):
    """构造携带指定配置键的 ConfigChanged 事件。"""
    return Event(EventType.ConfigChanged, ConfigChangeEventData(key=set(keys)))


async def _dispatch(instance, event):
    """按处理器类型走事件总线的同步或异步调用路径。"""
    handler = instance.__class__.handle_config_changed
    if inspect.iscoroutinefunction(handler):
        await eventmanager._EventManager__invoke_handler_by_type_async(handler, event)
    else:
        eventmanager._EventManager__invoke_handler_by_type_sync(handler, event)


def test_generated_handler_exposes_method_name(recorder):
    """生成的处理器 __name__ 必须与类上的方法名一致，保证事件总线能解析到实例方法。"""
    handler = recorder.__class__.handle_config_changed
    assert handler.__name__ == "handle_config_changed"
    assert handler.__qualname__ == f"{recorder.__class__.__name__}.handle_config_changed"


@pytest.mark.asyncio
async def test_config_changed_event_triggers_reload(recorder):
    """命中 CONFIG_WATCH 的配置变更事件必须触发子类重载逻辑。"""
    await _dispatch(recorder, _build_event({"TEST_RELOAD_KEY"}))
    assert recorder.reload_count == 1


@pytest.mark.asyncio
async def test_unrelated_config_key_skips_reload(recorder):
    """未监听的配置键变更不得触发重载。"""
    await _dispatch(recorder, _build_event({"OTHER_KEY"}))
    assert recorder.reload_count == 0


@pytest.mark.asyncio
async def test_resolve_falls_back_to_qualname_when_name_mismatched(monkeypatch):
    """处理器 __name__ 与类上方法名不一致时，事件总线应回退 __qualname__ 末段解析。"""
    instance = _ReloadRecorder()

    def wrapper(event):  # pylint: disable=unused-argument
        """模拟只同步了 __qualname__ 的动态处理器。"""
        instance.on_config_changed()

    wrapper.__module__ = _ReloadRecorder.__module__
    wrapper.__qualname__ = f"{_ReloadRecorder.__name__}.handle_config_changed"

    def resolver(owner_class):
        if owner_class is _ReloadRecorder:
            return [EventHandlerBinding(instance=instance, owner_name=owner_class.__name__)]
        return None

    monkeypatch.setattr(
        eventmanager,
        "_EventManager__handler_instance_resolvers",
        {"test_reload": resolver},
    )

    await eventmanager._EventManager__invoke_handler_by_type_async(
        wrapper, _build_event({"TEST_RELOAD_KEY"})
    )

    assert wrapper.__name__ == "wrapper"
    assert instance.reload_count == 1


def test_externally_managed_reload_class_does_not_register_listener(monkeypatch):
    """外部统一管理配置生命周期时，Mixin 保留重载能力但不重复绑定事件。"""
    registrations = []
    monkeypatch.setattr(
        eventmanager,
        "add_event_listener",
        lambda *args, **kwargs: registrations.append((args, kwargs)),
    )

    class _ExternallyManagedReloadRecorder(ConfigReloadMixin):
        CONFIG_RELOAD_MANAGED_EXTERNALLY = True
        CONFIG_WATCH = {"TEST_RELOAD_KEY"}

        def on_config_changed(self):
            pass

    assert registrations == []
    assert "handle_config_changed" not in _ExternallyManagedReloadRecorder.__dict__
