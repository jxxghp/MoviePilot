from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.foundation.singleton import Singleton
from app.runtime.extensions.plugin.monitor import PluginMonitorController
from app.runtime.extensions.plugin.system import reset_plugin_system
from app.runtime.extensions.plugin_manager import PluginManager
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
    """插件运行时和动态路由就绪后，启动层才允许文件监控接收变化。"""
    order: list[str] = []
    manager = MagicMock()
    manager.start.side_effect = lambda: order.append("plugins")
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

    assert order == ["services", "plugins", "routes", "monitor"]


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
