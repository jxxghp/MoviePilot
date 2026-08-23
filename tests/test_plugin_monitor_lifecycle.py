import asyncio
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.foundation.singleton import Singleton
from app.runtime.extensions.plugin.dependency import (
    PluginDependencyClassification,
    PluginDependencyInstallResult,
)
from app.runtime.extensions.plugin.monitor import (
    PluginChangeMonitor,
    PluginMonitorController,
)
from app.runtime.extensions.plugin.system import reset_plugin_system
from app.runtime.extensions.plugin_manager import PluginManager
from app.schemas.plugin import PluginRuntimeStatus
from app.startup import plugins_initializer


def _reset_plugin_manager() -> None:
    """清除插件管理器单例，保证构造时序测试彼此隔离。"""
    Singleton._instances.pop((PluginManager, (), frozenset()), None)


@pytest.mark.parametrize(
    ("dev", "auto_reload"),
    ((True, False), (False, True)),
)
def test_plugin_manager_constructor_does_not_start_monitor_before_runtime(
    monkeypatch,
    dev: bool,
    auto_reload: bool,
) -> None:
    """组合根尚未装配时，构造插件管理器不得提前启动开发监控线程。"""
    _reset_plugin_manager()
    reset_plugin_system()
    start = MagicMock()
    monkeypatch.setattr(
        "app.runtime.extensions.plugin_manager.settings",
        SimpleNamespace(
            DEV=dev,
            PLUGIN_AUTO_RELOAD=auto_reload,
            ROOT_PATH=MagicMock(),
        ),
    )
    monkeypatch.setattr(PluginMonitorController, "start", start)

    PluginManager()

    start.assert_not_called()
    _reset_plugin_manager()


def test_init_plugins_starts_monitor_after_runtime_and_routes(monkeypatch) -> None:
    """启动阶段只加载依赖已就绪的插件，再开放路由和文件监控。"""
    order: list[str] = []
    manager = MagicMock()
    manager.classify_plugins.return_value = PluginDependencyClassification(
        ready=("ReadyPlugin",),
        missing_dependencies=("DependencyPending",),
        missing_source=("SourcePending",),
    )
    manager.start.side_effect = lambda plugin_id: order.append(f"plugin:{plugin_id}")
    manager.start_monitor.side_effect = lambda: order.append("monitor")
    monkeypatch.setattr(
        plugins_initializer,
        "configure_plugin_services",
        lambda: order.append("services"),
    )
    monkeypatch.setattr(plugins_initializer, "PluginManager", lambda: manager)
    monkeypatch.setattr(
        plugins_initializer,
        "register_plugin_api",
        lambda: order.append("routes"),
    )

    plugins_initializer.init_plugins()

    assert order == ["services", "plugin:ReadyPlugin", "routes", "monitor"]
    manager.set_plugin_settling.assert_called_once_with(True)


def test_plugin_manager_projects_dependency_classification_to_runtime_status() -> None:
    """真实管理器按分类字段写入三类启动状态，避免测试替身掩盖字段漂移。"""
    _reset_plugin_manager()
    manager = PluginManager()

    manager.apply_plugin_dependency_classification(
        PluginDependencyClassification(
            ready=("ReadyPlugin",),
            missing_dependencies=("DependencyPending",),
            missing_source=("SourcePending",),
        )
    )

    assert manager.get_plugin_runtime_statuses() == {
        "ReadyPlugin": PluginRuntimeStatus.READY,
        "DependencyPending": PluginRuntimeStatus.DEPENDENCY_PENDING,
        "SourcePending": PluginRuntimeStatus.SOURCE_MISSING,
    }
    _reset_plugin_manager()


def test_plugin_manager_promotes_running_dependency_after_recovery() -> None:
    """依赖恢复后，运行中的插件状态必须允许后台流程触发重载。"""
    _reset_plugin_manager()
    manager = PluginManager()
    manager._plugin_registry.running["DependencyRecovered"] = object()
    manager._plugin_registry.set_runtime_status(
        "DependencyRecovered",
        PluginRuntimeStatus.DEPENDENCY_PENDING,
    )

    manager.apply_plugin_dependency_classification(
        PluginDependencyClassification(
            ready=("DependencyRecovered",),
            missing_dependencies=(),
            missing_source=(),
        )
    )

    assert manager.get_plugin_runtime_statuses()["DependencyRecovered"] is (
        PluginRuntimeStatus.READY
    )
    _reset_plugin_manager()


def _patch_sync_plugins(monkeypatch, manager: MagicMock) -> MagicMock:
    """隔离后台执行器并返回动态路由注册替身。"""
    async def execute(_loop, task_func, _task_name):
        return task_func()

    register = MagicMock()
    monkeypatch.setattr(plugins_initializer, "configure_plugin_services", lambda: None)
    monkeypatch.setattr(plugins_initializer, "PluginManager", lambda: manager)
    monkeypatch.setattr(plugins_initializer, "execute_task", execute)
    monkeypatch.setattr(plugins_initializer, "register_plugin_api", register)
    dependency_result = (
        manager.async_install_plugin_missing_dependencies_with_status.return_value
    )
    manager.async_install_plugin_missing_dependencies_with_status = AsyncMock(
        return_value=dependency_result,
    )
    manager.get_plugin_runtime_statuses.return_value = {}
    return register


@pytest.mark.asyncio
async def test_sync_plugins_activates_ready_plugins_when_dependencies_fail(
    monkeypatch,
) -> None:
    """依赖恢复失败时仍激活无关的已就绪插件。"""
    manager = MagicMock()
    manager.sync.return_value = ["demo"]
    manager.async_install_plugin_missing_dependencies_with_status.return_value = (
        PluginDependencyInstallResult(missing=["demo>=1"], success=False)
    )
    manager.classify_plugins.return_value = PluginDependencyClassification(
        ready=("ReadyPlugin",),
        missing_dependencies=("DependencyPending",),
        missing_source=(),
    )
    manager.running_plugins = {}
    register = _patch_sync_plugins(monkeypatch, manager)

    assert await plugins_initializer.sync_plugins() is True

    manager.start.assert_called_once_with("ReadyPlugin")
    manager.reload_plugin.assert_not_called()
    register.assert_called_once_with("ReadyPlugin")


@pytest.mark.asyncio
async def test_sync_plugins_loads_only_plugins_that_become_ready(
    monkeypatch,
) -> None:
    """后台依赖恢复后只启动尚未运行且当前已就绪的插件。"""
    manager = MagicMock()
    manager.sync.return_value = []
    manager.async_install_plugin_missing_dependencies_with_status.return_value = (
        PluginDependencyInstallResult(missing=["demo>=1"], success=True)
    )
    manager.classify_plugins.return_value = PluginDependencyClassification(
        ready=("ReadyPlugin", "DependencyRecovered"),
        missing_dependencies=(),
        missing_source=("SourcePending",),
    )
    running = {"ReadyPlugin": object()}
    manager.running_plugins = running

    def start(plugin_id: str) -> None:
        running[plugin_id] = object()

    manager.start.side_effect = start
    register = _patch_sync_plugins(monkeypatch, manager)

    assert await plugins_initializer.sync_plugins() is True

    manager.start.assert_called_once_with("DependencyRecovered")
    manager.reload_plugin.assert_not_called()
    register.assert_called_once_with("DependencyRecovered")


@pytest.mark.asyncio
async def test_sync_plugins_reloads_only_updated_running_plugins(monkeypatch) -> None:
    """源码同步只重载对应运行实例，不重启其他插件。"""
    manager = MagicMock()
    manager.sync.return_value = ["UpdatedPlugin"]
    manager.async_install_plugin_missing_dependencies_with_status.return_value = (
        PluginDependencyInstallResult(missing=[], success=True)
    )
    manager.classify_plugins.return_value = PluginDependencyClassification(
        ready=("StablePlugin", "UpdatedPlugin"),
        missing_dependencies=(),
        missing_source=(),
    )
    manager.running_plugins = {
        "StablePlugin": object(),
        "UpdatedPlugin": object(),
    }
    register = _patch_sync_plugins(monkeypatch, manager)

    assert await plugins_initializer.sync_plugins() is True

    manager.reload_plugin.assert_called_once_with("UpdatedPlugin")
    manager.start.assert_not_called()
    register.assert_called_once_with("UpdatedPlugin")


@pytest.mark.asyncio
async def test_sync_plugins_reloads_running_plugin_after_dependency_recovery(
    monkeypatch,
) -> None:
    """依赖恢复后，已运行的旧实例必须切换到新源码。"""
    manager = MagicMock()
    manager.sync.return_value = []
    manager.async_install_plugin_missing_dependencies_with_status.return_value = (
        PluginDependencyInstallResult(missing=["demo>=1"], success=True)
    )
    manager.classify_plugins.return_value = PluginDependencyClassification(
        ready=("DependencyRecovered",),
        missing_dependencies=(),
        missing_source=(),
    )
    manager.running_plugins = {"DependencyRecovered": object()}
    register = _patch_sync_plugins(monkeypatch, manager)
    manager.get_plugin_runtime_statuses.return_value = {
        "DependencyRecovered": PluginRuntimeStatus.DEPENDENCY_PENDING,
    }

    assert await plugins_initializer.sync_plugins() is True

    manager.reload_plugin.assert_called_once_with("DependencyRecovered")
    manager.start.assert_not_called()
    register.assert_called_once_with("DependencyRecovered")


@pytest.mark.asyncio
async def test_sync_plugins_keeps_runtime_when_nothing_changed(monkeypatch) -> None:
    """源码和依赖均无变化时保留首次初始化结果。"""
    manager = MagicMock()
    manager.sync.return_value = []
    manager.async_install_plugin_missing_dependencies_with_status.return_value = (
        PluginDependencyInstallResult(missing=[], success=True)
    )
    manager.classify_plugins.return_value = PluginDependencyClassification(
        ready=("ReadyPlugin",),
        missing_dependencies=(),
        missing_source=(),
    )
    manager.running_plugins = {"ReadyPlugin": object()}
    register = _patch_sync_plugins(monkeypatch, manager)

    assert await plugins_initializer.sync_plugins() is False

    manager.start.assert_not_called()
    manager.reload_plugin.assert_not_called()
    register.assert_not_called()


@pytest.mark.asyncio
async def test_sync_plugins_keeps_event_loop_responsive_during_activation(
    monkeypatch,
) -> None:
    """插件初始化运行在线程池时，Web 事件循环仍可继续调度。"""
    manager = MagicMock()
    manager.sync.return_value = []
    manager.async_install_plugin_missing_dependencies_with_status.return_value = (
        PluginDependencyInstallResult(missing=[], success=True)
    )
    manager.classify_plugins.return_value = PluginDependencyClassification(
        ready=("SlowPlugin",),
        missing_dependencies=(),
        missing_source=(),
    )
    manager.running_plugins = {}
    activation_started = threading.Event()

    def slow_start(_plugin_id: str) -> None:
        activation_started.set()
        time.sleep(0.1)

    manager.start.side_effect = slow_start
    manager.async_install_plugin_missing_dependencies_with_status = AsyncMock(
        return_value=PluginDependencyInstallResult(missing=[], success=True),
    )
    monkeypatch.setattr(plugins_initializer, "configure_plugin_services", lambda: None)
    monkeypatch.setattr(plugins_initializer, "PluginManager", lambda: manager)
    monkeypatch.setattr(plugins_initializer, "register_plugin_api", MagicMock())
    monkeypatch.setattr(
        plugins_initializer.global_vars,
        "CURRENT_EVENT_LOOP",
        asyncio.get_running_loop(),
    )

    sync_task = asyncio.create_task(plugins_initializer.sync_plugins())
    assert await asyncio.to_thread(activation_started.wait, 1)
    assert sync_task.done() is False
    assert await sync_task is True


@pytest.mark.parametrize(
    ("dev", "auto_reload", "expected_calls"),
    ((True, False, 1), (False, True, 1), (False, False, 0)),
)
def test_start_monitor_respects_runtime_configuration(
    monkeypatch,
    dev: bool,
    auto_reload: bool,
    expected_calls: int,
) -> None:
    """首次启动只在开发模式或插件自动重载启用时创建监控线程。"""
    _reset_plugin_manager()
    reset_plugin_system()
    monkeypatch.setattr(
        "app.runtime.extensions.plugin_manager.settings",
        SimpleNamespace(
            DEV=dev,
            PLUGIN_AUTO_RELOAD=auto_reload,
            ROOT_PATH=MagicMock(),
        ),
    )
    manager = PluginManager()
    start = MagicMock()
    manager._plugin_monitor.start = start

    manager.start_monitor()

    assert start.call_count == expected_calls
    _reset_plugin_manager()


def test_plugin_monitor_waits_until_dependency_settlement(monkeypatch) -> None:
    """后台依赖收敛期间不启动文件监控，避免源码写入触发重复重载。"""
    _reset_plugin_manager()
    reset_plugin_system()
    monkeypatch.setattr(
        "app.runtime.extensions.plugin_manager.settings",
        SimpleNamespace(
            DEV=True,
            PLUGIN_AUTO_RELOAD=False,
            ROOT_PATH=MagicMock(),
        ),
    )
    manager = PluginManager()
    start = MagicMock()
    reload_monitor = MagicMock()
    manager._plugin_monitor.start = start
    manager._plugin_monitor.reload = reload_monitor

    manager.set_plugin_settling(True)
    manager.start_monitor()
    manager.reload_monitor()

    start.assert_not_called()
    reload_monitor.assert_called_once_with(enabled=False)

    manager.set_plugin_settling(False)
    manager.start_monitor()

    start.assert_called_once_with()
    _reset_plugin_manager()


def test_plugin_monitor_skips_installing_plugin_until_package_write_finishes(tmp_path) -> None:
    """安装替换目录期间，文件事件不得抢先导入未完成的插件包。"""
    reload_plugin = MagicMock()
    monitor = PluginChangeMonitor(
        runtime_root=tmp_path,
        local_roots=lambda: [],
        stop_event=threading.Event(),
        recent_sync={},
        federated_change=lambda _path: None,
        runtime_plugin=lambda _path: "DemoPlugin",
        local_candidate=lambda _path: None,
        sync_local=MagicMock(),
        reload_plugin=reload_plugin,
        dependency_manifest_status=lambda _path: None,
        watch=lambda *_args, **_kwargs: (),
        log=MagicMock(),
        monitor_suppressed=lambda plugin_id: plugin_id.lower() == "demoplugin",
    )

    monitor._process_changes({("modified", str(tmp_path / "demo" / "plugin.py"))})

    reload_plugin.assert_not_called()


def test_plugin_monitor_suppression_is_reference_counted(monkeypatch) -> None:
    """同一插件的重叠写入必须等最后一个事务退出后才解除监控抑制。"""
    _reset_plugin_manager()
    reset_plugin_system()
    monkeypatch.setattr(
        "app.runtime.extensions.plugin_manager.settings",
        SimpleNamespace(
            DEV=False,
            PLUGIN_AUTO_RELOAD=False,
            ROOT_PATH=MagicMock(),
        ),
    )
    manager = PluginManager()

    with manager.suppress_plugin_monitor("DemoPlugin"):
        assert manager.is_plugin_monitor_suppressed("demoplugin") is True
        with manager.suppress_plugin_monitor("demoplugin"):
            assert manager.is_plugin_monitor_suppressed("DemoPlugin") is True
        assert manager.is_plugin_monitor_suppressed("DemoPlugin") is True

    assert manager.is_plugin_monitor_suppressed("DemoPlugin") is False
    _reset_plugin_manager()


def test_config_change_reloads_monitor(monkeypatch) -> None:
    """配置热更新继续使用重建语义，不复用首次启动入口。"""
    _reset_plugin_manager()
    reset_plugin_system()
    monkeypatch.setattr(
        "app.runtime.extensions.plugin_manager.settings",
        SimpleNamespace(
            DEV=False,
            PLUGIN_AUTO_RELOAD=False,
            ROOT_PATH=MagicMock(),
        ),
    )
    manager = PluginManager()
    reload_monitor = MagicMock()
    manager.reload_monitor = reload_monitor

    manager.on_config_changed()

    reload_monitor.assert_called_once_with()
    _reset_plugin_manager()


def test_stop_plugins_stops_monitor_before_plugin_runtime(monkeypatch) -> None:
    """关闭时先隔离文件变化，再停止插件实例。"""
    order: list[str] = []
    manager = MagicMock()
    manager.stop_monitor.side_effect = lambda: order.append("monitor")
    manager.stop.side_effect = lambda: order.append("plugins")
    monkeypatch.setattr(plugins_initializer, "PluginManager", lambda: manager)

    plugins_initializer.stop_plugins()

    assert order == ["monitor", "plugins"]


def test_stop_plugins_still_stops_runtime_when_monitor_stop_fails(monkeypatch) -> None:
    """监控线程停止异常不得阻止插件实例释放资源。"""
    manager = MagicMock()
    manager.stop_monitor.side_effect = RuntimeError("monitor stop failed")
    monkeypatch.setattr(plugins_initializer, "PluginManager", lambda: manager)

    plugins_initializer.stop_plugins()

    manager.stop.assert_called_once_with()
