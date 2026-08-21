"""插件安装事实与运行状态投影测试。"""

from types import SimpleNamespace
from typing import Iterator

import pytest

from app.foundation.singleton import Singleton
from app.runtime.extensions.plugin_manager import PluginManager
from app.schemas.plugin import PluginRuntimeStatus


@pytest.fixture
def plugin_manager() -> Iterator[PluginManager]:
    """构造隔离的插件管理器实例，避免单例状态污染其它用例。"""
    Singleton._instances.pop((PluginManager, (), frozenset()), None)
    manager = PluginManager()
    yield manager
    Singleton._instances.pop((PluginManager, (), frozenset()), None)


def test_installed_catalog_keeps_plugins_that_are_not_loaded(
    monkeypatch,
    plugin_manager: PluginManager,
) -> None:
    """已安装清单中的插件即使缺依赖或源码也必须保留可观察卡片。"""

    class ActivePlugin:
        plugin_name = "已运行插件"
        plugin_version = "1.0.0"
        plugin_order = 0

    installed_ids = ["ActivePlugin", "DependencyPending", "SourceMissing"]
    monkeypatch.setattr(
        "app.runtime.extensions.plugin_manager.get_plugin_storage",
        lambda: SimpleNamespace(read=lambda _key: list(installed_ids)),
    )
    plugin_manager._plugins["ActivePlugin"] = ActivePlugin
    plugin_manager._running_plugins["ActivePlugin"] = SimpleNamespace(
        get_state=lambda: True
    )
    for plugin_id, status in (
        ("ActivePlugin", PluginRuntimeStatus.ACTIVE),
        ("DependencyPending", PluginRuntimeStatus.DEPENDENCY_PENDING),
        ("SourceMissing", PluginRuntimeStatus.SOURCE_MISSING),
    ):
        plugin_manager._plugin_registry.set_runtime_status(plugin_id, status)

    plugins = plugin_manager.get_installed_plugins()

    assert [plugin.id for plugin in plugins] == installed_ids
    assert [plugin.runtime_status for plugin in plugins] == [
        PluginRuntimeStatus.ACTIVE,
        PluginRuntimeStatus.DEPENDENCY_PENDING,
        PluginRuntimeStatus.SOURCE_MISSING,
    ]
    assert plugins[1].plugin_name == "DependencyPending"
    assert plugins[2].installed is True


def test_local_catalog_projects_runtime_status(
    monkeypatch,
    plugin_manager: PluginManager,
) -> None:
    """本地插件投影带上运行状态，前端无需再次查询即可区分未加载原因。"""

    class DemoPlugin:
        plugin_name = "演示插件"
        plugin_version = "1.0.0"
        plugin_order = 0

    monkeypatch.setattr(
        "app.runtime.extensions.plugin_manager.get_plugin_storage",
        lambda: SimpleNamespace(read=lambda _key: ["DemoPlugin"]),
    )
    plugin_manager._plugins["DemoPlugin"] = DemoPlugin
    plugin_manager._plugin_registry.set_runtime_status(
        "DemoPlugin", PluginRuntimeStatus.LOAD_FAILED
    )

    plugins = plugin_manager.get_local_plugins()

    assert [plugin.runtime_status for plugin in plugins] == [
        PluginRuntimeStatus.LOAD_FAILED
    ]
