"""插件安装事实与运行状态投影测试。"""

from types import SimpleNamespace

from app.runtime.extensions.plugin.catalog import PluginCatalogFacade
from app.schemas.plugin import PluginRuntimeStatus
from app.schemas.types import SystemConfigKey


def test_installed_catalog_keeps_plugins_that_are_not_loaded():
    """已安装清单中的插件即使缺依赖或源码也必须保留可观察卡片。"""
    class ActivePlugin:
        plugin_name = "已运行插件"
        plugin_version = "1.0.0"
        plugin_order = 0

    active_instance = SimpleNamespace(get_state=lambda: True)
    statuses = {
        "ActivePlugin": PluginRuntimeStatus.ACTIVE,
        "DependencyPending": PluginRuntimeStatus.DEPENDENCY_PENDING,
        "SourceMissing": PluginRuntimeStatus.SOURCE_MISSING,
    }
    facade = PluginCatalogFacade(
        classes=lambda: {"ActivePlugin": ActivePlugin},
        running=lambda: {"ActivePlugin": active_instance},
        storage=lambda: SimpleNamespace(
            read=lambda key: [
                "ActivePlugin",
                "DependencyPending",
                "SourceMissing",
            ] if key is SystemConfigKey.UserInstalledPlugins else None,
        ),
        system=lambda: SimpleNamespace(),
        market_catalog=lambda: None,
        market_loader=lambda *_args, **_kwargs: [],
        async_market_loader=lambda *_args, **_kwargs: [],
        map_plugin=lambda **_kwargs: None,
        auth_checker=lambda **_kwargs: True,
        plugin_attr=lambda _plugin_id, _attr: None,
        plugin_instance=lambda _plugin_id: None,
        plugin_instances=lambda: {},
        runtime_status=statuses.get,
        log=SimpleNamespace(error=lambda *_args: None, info=lambda *_args: None),
    )

    plugins = facade.installed()

    assert [plugin.id for plugin in plugins] == [
        "ActivePlugin",
        "DependencyPending",
        "SourceMissing",
    ]
    assert [plugin.runtime_status for plugin in plugins] == [
        PluginRuntimeStatus.ACTIVE,
        PluginRuntimeStatus.DEPENDENCY_PENDING,
        PluginRuntimeStatus.SOURCE_MISSING,
    ]
    assert plugins[1].plugin_name == "DependencyPending"
    assert plugins[2].installed is True
