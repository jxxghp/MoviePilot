"""插件实例日志上下文接入点契约测试。

覆盖四类宿主受控调用点：插件实例的构造与 `init_plugin`
（`PluginLifecycle.start`/`initialize`）、事件处理器回调（`EventDispatcher`
的四个 invoke 方法）、定时服务回调（`SchedulerReconcileOwner.update_plugin_job`）、
HTTP API 端点回调（`PluginProjection.apis`）。每个用例只断言绑定期间
`current_plugin_instance_id()` 能读到发起调用的实例 ID，且调用结束后上下文
必须恢复为未绑定，不污染后续用例。
"""

from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import app.scheduler.reconcile as reconcile_module
from app.runtime.event.binding import EventBindingResolver, EventHandlerBinding
from app.runtime.event.dispatch import EventDispatcher
from app.runtime.extensions.plugin.database import PluginDatabase
from app.runtime.extensions.plugin.lifecycle import PluginLifecycle
from app.runtime.extensions.plugin.projection import PluginProjection
from app.runtime.log import current_plugin_instance_id
from app.scheduler.reconcile import SchedulerReconcileOwner

# ---------------------------------------------------------------------------
# 1. 插件实例构造与 init_plugin
# ---------------------------------------------------------------------------


def _lifecycle(*, plugins):
    """构造隔离外部事件和模块清理的生命周期实例。"""
    classes: dict = {}
    running: dict = {}
    lifecycle = PluginLifecycle(
        classes=classes,
        running=running,
        load_plugins=lambda _pid, _installed, _check, _version=None: list(plugins),
        installed_plugins=lambda: ["DemoPluginWork"],
        plugin_config=lambda _pid: {},
        auth_checker=lambda _plugin: True,
        clear_modules=MagicMock(),
        clear_tools=MagicMock(),
        enable_events=MagicMock(),
        disable_events=MagicMock(),
        runtime_status_writer=MagicMock(),
        database=lambda: PluginDatabase(),
        log=MagicMock(),
        event_sender=MagicMock(),
    )
    return lifecycle, classes, running


def test_lifecycle_start_binds_instance_during_construct_and_init_plugin():
    """构造与 init_plugin 期间应能读到发起它的实例 ID，start 返回后上下文必须清空。"""
    seen: list[str | None] = []

    class _Plugin:
        plugin_name = "演示插件"
        plugin_version = "1.0.0"

        def __init__(self) -> None:
            seen.append(("construct", current_plugin_instance_id()))

        def init_plugin(self, _config: dict) -> None:
            seen.append(("init", current_plugin_instance_id()))

        @staticmethod
        def get_state() -> bool:
            return True

    _Plugin.__name__ = "DemoPluginWork"
    lifecycle, _classes, _running = _lifecycle(plugins=[_Plugin])

    lifecycle.start("DemoPluginWork")

    assert seen == [
        ("construct", "DemoPluginWork"),
        ("init", "DemoPluginWork"),
    ]
    assert current_plugin_instance_id() is None


def test_lifecycle_initialize_binds_instance_during_reinit():
    """公开 init_plugin（配置页重新生效）同样要绑定发起它的实例。"""
    seen: list[str | None] = []

    class _Plugin:
        def init_plugin(self, _config: dict) -> None:
            seen.append(current_plugin_instance_id())

        @staticmethod
        def get_state() -> bool:
            return True

    running = {"DemoPluginWork": _Plugin()}
    lifecycle = PluginLifecycle(
        classes={},
        running=running,
        load_plugins=lambda *_a, **_kw: [],
        installed_plugins=lambda: [],
        plugin_config=lambda _pid: {},
        auth_checker=lambda _plugin: True,
        clear_modules=MagicMock(),
        clear_tools=MagicMock(),
        enable_events=MagicMock(),
        disable_events=MagicMock(),
        runtime_status_writer=MagicMock(),
        database=lambda: PluginDatabase(),
        log=MagicMock(),
        event_sender=MagicMock(),
    )

    lifecycle.initialize("DemoPluginWork", {"enable": True})

    assert seen == ["DemoPluginWork"]
    assert current_plugin_instance_id() is None


# ---------------------------------------------------------------------------
# 2. 事件处理器回调
# ---------------------------------------------------------------------------


class _FakeEventType:
    """提供 dispatch 内部读取的 `.value` 属性。"""

    value = "test.event"


class _FakeEvent:
    """携带 dispatch 内部读取的最小事件属性集。"""

    correlation_id = None
    event_type = _FakeEventType()


def _dispatcher(resolvers: dict) -> EventDispatcher:
    """构造只依赖真实 EventBindingResolver 的最小事件调度器。"""
    binding_resolver = EventBindingResolver(lock=threading.Lock(), resolvers=lambda: resolvers)
    return EventDispatcher(
        registry=MagicMock(),
        binding_resolver=binding_resolver,
        event_factory=MagicMock(),
        error_handler=MagicMock(side_effect=AssertionError("handler must not error")),
        async_handle_sink=MagicMock(),
        sync_handle_sink=MagicMock(),
    )


class _PluginEventHandler:
    """模拟虚拟实例克隆类：`__name__` 与运行实例 ID 相同。"""

    def __init__(self) -> None:
        """记录事件处理期间观察到的实例上下文。"""
        self.seen: list[str | None] = []

    def on_event(self, _event: object) -> None:
        """记录当前绑定的实例 ID。"""
        self.seen.append(current_plugin_instance_id())

    async def on_event_async(self, _event: object) -> None:
        """异步处理器同样记录当前绑定的实例 ID。"""
        self.seen.append(current_plugin_instance_id())


def test_invoke_sync_binds_owning_instance():
    """同步事件处理器执行期间应绑定声明它的插件实例。"""
    instance = _PluginEventHandler()
    _PluginEventHandler.__name__ = "DemoPluginWork"
    resolvers = {
        "plugins": lambda owner_class: (
            EventHandlerBinding(instance=instance, owner_name="Demo", run_sync_in_threadpool=True)
            if owner_class is _PluginEventHandler
            else None
        )
    }
    dispatcher = _dispatcher(resolvers)

    dispatcher.invoke_sync(_PluginEventHandler.on_event, _FakeEvent())

    assert instance.seen == ["DemoPluginWork"]
    assert current_plugin_instance_id() is None


def test_invoke_sync_strict_binds_owning_instance():
    """strict 变体同样要绑定实例，且失败时仍需正确复位上下文。"""
    instance = _PluginEventHandler()
    _PluginEventHandler.__name__ = "DemoPluginWork"
    resolvers = {
        "plugins": lambda owner_class: (
            EventHandlerBinding(instance=instance, owner_name="Demo")
            if owner_class is _PluginEventHandler
            else None
        )
    }
    dispatcher = _dispatcher(resolvers)

    dispatcher.invoke_sync_strict(_PluginEventHandler.on_event, _FakeEvent())

    assert instance.seen == ["DemoPluginWork"]
    assert current_plugin_instance_id() is None


@pytest.mark.asyncio
async def test_invoke_async_binds_owning_instance_for_coroutine_handler():
    """异步事件处理器（直接 await）执行期间应绑定声明它的插件实例。"""
    instance = _PluginEventHandler()
    _PluginEventHandler.__name__ = "DemoPluginWork"
    resolvers = {
        "plugins": lambda owner_class: (
            EventHandlerBinding(instance=instance, owner_name="Demo")
            if owner_class is _PluginEventHandler
            else None
        )
    }
    dispatcher = _dispatcher(resolvers)

    await dispatcher.invoke_async(_PluginEventHandler.on_event_async, _FakeEvent())

    assert instance.seen == ["DemoPluginWork"]
    assert current_plugin_instance_id() is None


@pytest.mark.asyncio
async def test_invoke_async_binds_owning_instance_across_threadpool_hop():
    """同步处理器经线程池执行时，绑定必须跨越 `run_in_threadpool` 的执行上下文切换。"""
    instance = _PluginEventHandler()
    _PluginEventHandler.__name__ = "DemoPluginWork"
    resolvers = {
        "plugins": lambda owner_class: (
            EventHandlerBinding(instance=instance, owner_name="Demo", run_sync_in_threadpool=True)
            if owner_class is _PluginEventHandler
            else None
        )
    }
    dispatcher = _dispatcher(resolvers)

    await dispatcher.invoke_async(_PluginEventHandler.on_event, _FakeEvent())

    assert instance.seen == ["DemoPluginWork"]
    assert current_plugin_instance_id() is None


def test_invoke_sync_does_not_bind_free_function_handler():
    """自由函数处理器不属于任何插件实例，不应绑定任何上下文。"""
    seen: list[str | None] = []

    def _free_handler(_event: object) -> None:
        seen.append(current_plugin_instance_id())

    dispatcher = _dispatcher({})

    dispatcher.invoke_sync(_free_handler, _FakeEvent())

    assert seen == [None]


# ---------------------------------------------------------------------------
# 3. 定时服务回调
# ---------------------------------------------------------------------------


def test_update_plugin_job_binds_instance_around_service_callback(monkeypatch):
    """插件定时服务被调度器实际调用时应绑定注册它的插件实例。"""
    seen: list[str | None] = []

    def _service_callback() -> None:
        seen.append(current_plugin_instance_id())

    fake_manager = SimpleNamespace(
        get_plugin_services=lambda pid: [
            {
                "id": "job1",
                "name": "演示任务",
                "func": _service_callback,
                "trigger": "interval",
                "kwargs": {"seconds": 60},
            }
        ],
        get_plugin_attr=lambda _pid, _attr: "演示插件",
    )
    monkeypatch.setattr(reconcile_module, "get_plugin_manager", lambda: fake_manager)

    owner = SchedulerReconcileOwner.__new__(SchedulerReconcileOwner)
    owner._scheduler = MagicMock()
    owner._lock = threading.RLock()
    owner._jobs = {}
    owner.start = MagicMock()
    owner.remove_plugin_job = lambda _pid, job_id=None: None
    owner._assign_job_generation = lambda _job_id, _job: None

    owner.update_plugin_job("DemoPluginWork")

    registered_job = owner._jobs["DemoPluginWork_job1"]
    registered_job["func"]()

    assert seen == ["DemoPluginWork"]
    assert current_plugin_instance_id() is None


# ---------------------------------------------------------------------------
# 4. HTTP API 端点回调
# ---------------------------------------------------------------------------


class _ApiEndpointPlugin:
    """声明一条会读取实例上下文的 HTTP API 路由的最小插件桩。"""

    plugin_name = "接口插件"

    def __init__(self) -> None:
        """初始化调用记录。"""
        self.seen: list[str | None] = []

    def get_state(self) -> bool:
        """插件始终启用。"""
        return True

    def get_name(self) -> str:
        """返回插件展示名称。"""
        return self.plugin_name

    def get_api(self) -> list[dict]:
        """声明一条状态查询路由，endpoint 绑定到本实例的方法。"""
        return [{"path": "/status", "endpoint": self.status, "methods": ["GET"]}]

    def status(self) -> dict:
        """处理状态查询请求期间记录当前绑定的实例 ID。"""
        self.seen.append(current_plugin_instance_id())
        return {"ok": True}


def test_projection_api_endpoint_binds_owning_instance_without_any_caller_context():
    """路由被 FastAPI 直接调用（不经过任何宿主受控调用点）时仍应绑定实例。"""
    plugin = _ApiEndpointPlugin()
    projection = PluginProjection({"DemoPluginWork": plugin})

    apis = projection.apis()

    assert current_plugin_instance_id() is None
    apis[0]["endpoint"]()

    assert plugin.seen == ["DemoPluginWork"]
    assert current_plugin_instance_id() is None
