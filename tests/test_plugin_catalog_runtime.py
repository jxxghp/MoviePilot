"""插件安装事实与运行状态投影测试。"""

from types import SimpleNamespace

import pytest

from app.runtime import log as log_module
from app.runtime.extensions.plugin.catalog import PluginCatalogFacade
from app.runtime.log import set_plugin_instance_log_level
from app.schemas.plugin import PluginInstance, PluginRuntimeStatus
from app.schemas.types import SystemConfigKey


@pytest.fixture(name="_isolated_log_overrides")
def fixture_isolated_log_overrides(monkeypatch):
    """隔离进程内日志等级覆盖缓存，避免与其他用例的实例 ID 相互污染。"""
    monkeypatch.setattr(log_module, "_plugin_level_overrides", {})


def _facade(**overrides):
    """按测试所需覆盖最小可用回调集合，构造一个 PluginCatalogFacade。"""
    defaults = dict(
        classes=lambda: {},
        running=lambda: {},
        storage=lambda: SimpleNamespace(
            read=lambda _key: None,
            write=lambda _key, _value: None,
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
        runtime_status=lambda _plugin_id: None,
        log=SimpleNamespace(error=lambda *_args: None, info=lambda *_args: None),
    )
    defaults.update(overrides)
    return PluginCatalogFacade(**defaults)


@pytest.mark.asyncio
async def test_async_online_normalizes_market_configuration(monkeypatch) -> None:
    """目录入口与候选库存使用相同的规范化市场列表。"""
    from app.runtime.extensions.plugin import catalog as catalog_module

    requested_markets: list[str] = []

    class FakeMarketCatalog:
        async def async_collect(self, **kwargs):
            requested_markets.extend(kwargs["markets"])
            return []

    monkeypatch.setattr(
        catalog_module,
        "get_runtime_setting",
        lambda key: {
            "PLUGIN_MARKET": (
                " https://github.com/Example/Plugins.git/ ,"
                "https://github.com/example/plugins"
            ),
            "VERSION_FLAG": "v3",
        }[key],
    )
    facade = PluginCatalogFacade(
        classes=lambda: {},
        running=lambda: {},
        storage=lambda: SimpleNamespace(read=lambda _key: []),
        system=lambda: SimpleNamespace(
            compatible_flags=lambda _flag: [],
        ),
        market_catalog=lambda: FakeMarketCatalog(),
        market_loader=lambda *_args, **_kwargs: [],
        async_market_loader=lambda *_args, **_kwargs: [],
        map_plugin=lambda **_kwargs: None,
        auth_checker=lambda **_kwargs: True,
        plugin_attr=lambda _plugin_id, _attr: None,
        plugin_instance=lambda _plugin_id: None,
        plugin_instances=lambda: {},
        runtime_status=lambda _plugin_id: None,
        log=SimpleNamespace(info=lambda *_args: None),
    )

    assert await facade.async_online() == []
    assert requested_markets == ["https://github.com/Example/Plugins"]


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


def test_local_repository_failure_does_not_break_catalog_projection():
    """本地仓索引读取失败只影响本地候选展示，不能拖垮整个插件目录。"""
    warnings: list[str] = []

    def local_candidates():
        raise RuntimeError("invalid local package")

    facade = PluginCatalogFacade(
        classes=lambda: {},
        running=lambda: {},
        storage=lambda: SimpleNamespace(read=lambda _key: []),
        system=lambda: SimpleNamespace(
            local_candidates=local_candidates,
        ),
        market_catalog=lambda: None,
        market_loader=lambda *_args, **_kwargs: [],
        async_market_loader=lambda *_args, **_kwargs: [],
        map_plugin=lambda **_kwargs: None,
        auth_checker=lambda **_kwargs: True,
        plugin_attr=lambda _plugin_id, _attr: None,
        plugin_instance=lambda _plugin_id: None,
        plugin_instances=lambda: {},
        runtime_status=lambda _plugin_id: None,
        log=SimpleNamespace(
            error=lambda *_args: None,
            info=lambda *_args: None,
            warning=warnings.append,
        ),
    )

    assert facade.local_repository() == []
    assert warnings == [
        "读取本地插件仓候选失败，已跳过本地目录展示：invalid local package"
    ]
    with pytest.raises(RuntimeError, match="invalid local package"):
        facade.local_repository(raise_errors=True)


def test_local_projects_effective_log_level_for_virtual_instance(
    _isolated_log_overrides,
):
    """分身设了日志等级覆盖时，生效等级要如实投影到它的卡片上。"""
    set_plugin_instance_log_level("CatalogOverlayVirtual", "DEBUG")

    class DemoPlugin:
        plugin_name = "Demo"
        plugin_order = 0

    instance = PluginInstance(
        instance_id="CatalogOverlayVirtual",
        source_plugin_id="DemoPlugin",
    )
    facade = _facade(
        classes=lambda: {"CatalogOverlayVirtual": DemoPlugin},
        plugin_instance=lambda plugin_id: (
            instance if plugin_id == "CatalogOverlayVirtual" else None
        ),
        plugin_instances=lambda: {"CatalogOverlayVirtual": instance},
    )

    plugin = facade.local()[0]

    assert plugin.log_level_effective == "DEBUG"


def test_local_reports_no_log_override_when_instance_follows_global_level(
    _isolated_log_overrides,
):
    """没有覆盖的分身实例不应带任何日志等级徽标信息。"""
    class DemoPlugin:
        plugin_name = "Demo"
        plugin_order = 0

    instance = PluginInstance(
        instance_id="CatalogOverlayDefaultVirtual",
        source_plugin_id="DemoPlugin",
    )
    facade = _facade(
        classes=lambda: {"CatalogOverlayDefaultVirtual": DemoPlugin},
        plugin_instance=lambda plugin_id: (
            instance if plugin_id == "CatalogOverlayDefaultVirtual" else None
        ),
        plugin_instances=lambda: {"CatalogOverlayDefaultVirtual": instance},
    )

    plugin = facade.local()[0]

    assert plugin.log_level_effective is None


def test_local_projects_effective_log_level_for_physical_plugin(
    _isolated_log_overrides,
):
    """物理插件本体的覆盖同样要投影出来，实例 ID 就是插件 ID。

    ``is_instance``、``instance_mode`` 只看分身记录，不受日志等级覆盖影响，
    现有语义保持不变。
    """
    set_plugin_instance_log_level("CatalogOverlayHost", "WARNING")

    class DemoPlugin:
        plugin_name = "Demo"
        plugin_order = 0

    facade = _facade(classes=lambda: {"CatalogOverlayHost": DemoPlugin})

    plugin = facade.local()[0]

    assert plugin.is_instance is False
    assert plugin.instance_mode is None
    assert plugin.log_level_effective == "WARNING"


def test_installed_placeholder_projects_effective_log_level(
    _isolated_log_overrides,
):
    """未加载插件的占位卡片同样要投影生效日志等级。"""
    set_plugin_instance_log_level("CatalogOverlayPlaceholder", "ERROR")

    facade = _facade(
        storage=lambda: SimpleNamespace(
            read=lambda key: (
                ["CatalogOverlayPlaceholder"]
                if key is SystemConfigKey.UserInstalledPlugins
                else None
            )
        ),
    )

    plugin = facade.installed()[0]

    assert plugin.log_level_effective == "ERROR"
