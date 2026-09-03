"""ConfigReloadMixin 配置变更事件处理器解析的回归测试（Issue #6329）。

ConfigReloadMixin 动态生成的事件处理器，必须能被事件总线解析回实例方法，
不能因 __name__ 与类上方法名不一致而被静默跳过。
"""

import inspect
from unittest.mock import AsyncMock, Mock

import pytest

from app.adapters.cache.redis import AsyncRedisHelper, RedisHelper
from app.adapters.network.doh import DohHelper
from app.chain.scraping import ScrapingChain
from app.chain.transfer.facade import TransferChain
from app.foundation.singleton import Singleton, SingletonClass
from app.monitor.monitor import Monitor
from app.runtime.events import Event, EventHandlerBinding, eventmanager
from app.runtime.extensions.plugin.manager import PluginManager
from app.runtime.reload import ConfigReloadMixin
from app.runtime.state import SystemHelper
from app.scheduler.facade import Scheduler
from app.schemas.event import ConfigChangeEventData
from app.schemas.types import EventType
from app.startup.initializers.modules import (
    configure_config_reload_event_handler_resolver,
    configure_host_event_handler_resolver,
    get_config_reload_handler_providers,
)


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
            return EventHandlerBinding(instance=instance, owner_name=owner_class.__name__)
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


def _configure_lifespan_resolvers() -> None:
    """按启动组合顺序登记宿主和配置 owner resolver。"""
    configure_host_event_handler_resolver()
    configure_config_reload_event_handler_resolver()


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
            return EventHandlerBinding(instance=instance, owner_name=owner_class.__name__)
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


def test_config_reload_owner_is_skipped_without_lifecycle_resolver(monkeypatch):
    """非模块配置 owner 缺少 resolver 时不得被事件总线临时构造。"""
    helper = object.__new__(DohHelper)
    reload_config = Mock()
    helper.on_config_changed = reload_config
    monkeypatch.setattr(
        eventmanager,
        "_EventManager__handler_instance_resolvers",
        {},
    )

    eventmanager._EventManager__invoke_handler_by_type_sync(
        DohHelper.handle_config_changed,
        _build_event({"DOH_ENABLE"}),
    )

    reload_config.assert_not_called()
    assert (
        "app.adapters.network.doh.DohHelper.handle_config_changed"
        in eventmanager.unresolved_handler_bindings()
    )


def test_host_config_reload_resolver_binds_current_doh_owner(monkeypatch):
    """DoH 配置事件必须绑定到已由组合根物化的当前 Adapter。"""
    helper = object.__new__(DohHelper)
    reload_config = Mock()
    helper.on_config_changed = reload_config
    singleton_key = (DohHelper, (), frozenset())
    monkeypatch.setitem(Singleton._instances, singleton_key, helper)
    monkeypatch.setattr(
        eventmanager,
        "_EventManager__handler_instance_resolvers",
        {},
    )
    _configure_lifespan_resolvers()

    eventmanager._EventManager__invoke_handler_by_type_sync(
        DohHelper.handle_config_changed,
        _build_event({"DOH_ENABLE"}),
    )

    reload_config.assert_called_once_with()

    eventmanager._EventManager__invoke_handler_by_type_sync(
        DohHelper.handle_config_changed,
        _build_event({"OTHER_KEY"}),
    )

    reload_config.assert_called_once_with()


def test_host_resolver_binds_current_scraping_owner(monkeypatch):
    """宿主 resolver 必须把同步重载处理器绑定到当前 Chain 单例。"""
    chain = object.__new__(ScrapingChain)
    reload_config = Mock()
    chain.on_config_changed = reload_config
    singleton_key = (ScrapingChain, (), frozenset())
    monkeypatch.setitem(Singleton._instances, singleton_key, chain)
    monkeypatch.setattr(
        eventmanager,
        "_EventManager__handler_instance_resolvers",
        {},
    )
    configure_host_event_handler_resolver()

    eventmanager._EventManager__invoke_handler_by_type_sync(
        ScrapingChain.handle_config_changed,
        _build_event({"ScrapingSwitchs"}),
    )

    reload_config.assert_called_once_with()


@pytest.mark.asyncio
async def test_host_resolver_binds_current_scheduler_owner(monkeypatch):
    """宿主 resolver 必须把异步重载处理器绑定到当前 Scheduler 单例。"""
    scheduler = object.__new__(Scheduler)
    reload_config = AsyncMock()
    scheduler.on_config_changed = reload_config
    monkeypatch.setitem(SingletonClass._instances, Scheduler, scheduler)
    monkeypatch.setattr(
        eventmanager,
        "_EventManager__handler_instance_resolvers",
        {},
    )
    configure_host_event_handler_resolver()

    await eventmanager._EventManager__invoke_handler_by_type_async(
        Scheduler.handle_config_changed,
        _build_event({"DEV"}),
    )

    reload_config.assert_awaited_once_with()


def test_config_reload_resolver_reads_latest_singleton_owner(monkeypatch):
    """resolver 每次读取当前单例，不能保留已经换代的 Redis 实例。"""
    first = object.__new__(RedisHelper)
    first_reload = Mock()
    first.on_config_changed = first_reload
    second = object.__new__(RedisHelper)
    second_reload = Mock()
    second.on_config_changed = second_reload
    singleton_key = (RedisHelper, (), frozenset())
    monkeypatch.setitem(Singleton._instances, singleton_key, first)
    monkeypatch.setattr(
        eventmanager,
        "_EventManager__handler_instance_resolvers",
        {},
    )
    _configure_lifespan_resolvers()

    eventmanager._EventManager__invoke_handler_by_type_sync(
        RedisHelper.handle_config_changed,
        _build_event({"CACHE_BACKEND_URL"}),
    )
    monkeypatch.setitem(Singleton._instances, singleton_key, second)
    eventmanager._EventManager__invoke_handler_by_type_sync(
        RedisHelper.handle_config_changed,
        _build_event({"CACHE_BACKEND_URL"}),
    )

    first_reload.assert_called_once_with()
    second_reload.assert_called_once_with()


@pytest.mark.asyncio
async def test_config_reload_resolver_invokes_current_async_redis_owner(monkeypatch):
    """异步配置处理器必须绑定当前 Redis owner，并保留键筛选。"""
    helper = object.__new__(AsyncRedisHelper)
    reload_config = AsyncMock()
    helper.on_config_changed = reload_config
    singleton_key = (AsyncRedisHelper, (), frozenset())
    monkeypatch.setitem(Singleton._instances, singleton_key, helper)
    monkeypatch.setattr(
        eventmanager,
        "_EventManager__handler_instance_resolvers",
        {},
    )
    _configure_lifespan_resolvers()

    await eventmanager._EventManager__invoke_handler_by_type_async(
        AsyncRedisHelper.handle_config_changed,
        _build_event({"CACHE_REDIS_MAX_CONNECTIONS"}),
    )
    await eventmanager._EventManager__invoke_handler_by_type_async(
        AsyncRedisHelper.handle_config_changed,
        _build_event({"OTHER_KEY"}),
    )

    reload_config.assert_awaited_once_with()


def test_config_reload_resolver_does_not_materialize_lazy_transfer_owner(
    monkeypatch,
):
    """尚未创建的整理链只声明跳过，配置事件不得启动线程资源。"""
    singleton_key = (TransferChain, (), frozenset())
    monkeypatch.delitem(Singleton._instances, singleton_key, raising=False)
    monkeypatch.setattr(
        eventmanager,
        "_EventManager__handler_instance_resolvers",
        {},
    )
    _configure_lifespan_resolvers()

    eventmanager._EventManager__invoke_handler_by_type_sync(
        TransferChain.handle_config_changed,
        _build_event({"TRANSFER_THREADS"}),
    )

    assert TransferChain.get_existing_instance() is None


def test_plugin_manager_falls_through_plugin_resolver_to_current_owner(
    monkeypatch,
):
    """resolver 先注册时也应在插件 Runtime 创建后接管当前管理器。"""
    singleton_key = (PluginManager, (), frozenset())
    monkeypatch.delitem(Singleton._instances, singleton_key, raising=False)
    monkeypatch.setattr(
        eventmanager,
        "_EventManager__handler_instance_resolvers",
        {},
    )
    configure_config_reload_event_handler_resolver()

    manager = object.__new__(PluginManager)
    reload_config = Mock()
    manager.on_config_changed = reload_config
    monkeypatch.setitem(Singleton._instances, singleton_key, manager)
    plugin_resolver = Mock(return_value=None)
    eventmanager.register_handler_instance_resolver(
        "plugins",
        plugin_resolver,
    )

    eventmanager._EventManager__invoke_handler_by_type_sync(
        PluginManager.handle_config_changed,
        _build_event({"PLUGIN_AUTO_RELOAD"}),
    )

    plugin_resolver.assert_not_called()
    reload_config.assert_called_once_with()


def test_config_reload_resolver_registration_replaces_same_name(monkeypatch):
    """重复装配只替换当前 lifespan resolver，不累积并行绑定。"""
    monkeypatch.setattr(
        eventmanager,
        "_EventManager__handler_instance_resolvers",
        {},
    )

    configure_config_reload_event_handler_resolver()
    first = eventmanager._EventManager__handler_instance_resolvers[
        "config_reload"
    ]
    configure_config_reload_event_handler_resolver()

    assert set(eventmanager._EventManager__handler_instance_resolvers) == {
        "config_reload"
    }
    assert (
        eventmanager._EventManager__handler_instance_resolvers[
            "config_reload"
        ]
        is not first
    )


@pytest.mark.parametrize(
    ("owner_class", "registry", "singleton_key"),
    [
        (
            PluginManager,
            Singleton._instances,
            (PluginManager, (), frozenset()),
        ),
        (Monitor, SingletonClass._instances, Monitor),
    ],
)
def test_config_reload_provider_returns_lifecycle_owned_instance(
    monkeypatch,
    owner_class,
    registry,
    singleton_key,
):
    """管理器和监控器必须从各自生命周期注册表读取当前实例。"""
    instance = object.__new__(owner_class)
    monkeypatch.setitem(registry, singleton_key, instance)

    providers = get_config_reload_handler_providers()

    assert providers[owner_class]() is instance


def test_system_reload_provider_keeps_one_lifespan_instance() -> None:
    """无资源的系统配置处理器也由 resolver 闭包保持稳定 owner 身份。"""
    provider = get_config_reload_handler_providers()[SystemHelper]

    assert provider() is provider()
