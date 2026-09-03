"""插件安装事实与运行状态投影测试。"""

from types import SimpleNamespace

import pytest

from app.runtime import log as log_module
from app.runtime.extensions.plugin.catalog import PluginCatalogFacade
from app.runtime.log import set_plugin_instance_log_level
from app.schemas.plugin import PluginInstance, PluginRuntimeStatus
from app.schemas.types import SystemConfigKey


@pytest.fixture(name="_isolated_log_overrides", autouse=False)
def fixture_isolated_log_overrides(monkeypatch):
    """隔离进程内日志等级覆盖缓存，避免与其他用例的实例 ID 相互污染。"""
    monkeypatch.setattr(log_module, "_plugin_level_overrides", {})


def _facade(**overrides):
    """按测试所需覆盖最小可用回调集合，构造一个 PluginCatalogFacade。"""
    defaults = dict(
        classes=lambda: {},
        running=lambda: {},
        storage=lambda: SimpleNamespace(read=lambda _key: None),
        system=lambda: SimpleNamespace(),
        market_catalog=lambda: None,
        market_loader=lambda *_args, **_kwargs: [],
        async_market_loader=lambda *_args, **_kwargs: [],
        map_plugin=lambda **_kwargs: None,
        auth_checker=lambda **_kwargs: True,
        plugin_attr=lambda _plugin_id, _attr: None,
        plugin_instance=lambda _plugin_id: None,
        plugin_instances=lambda: {},
        host_instances=lambda: {},
        runtime_status=lambda _plugin_id: None,
        log=SimpleNamespace(error=lambda *_args: None, info=lambda *_args: None),
    )
    defaults.update(overrides)
    return PluginCatalogFacade(**defaults)


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
        host_instances=lambda: {},
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
        host_instances=lambda: {},
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


def test_local_projects_pinned_version_default_target_and_log_level_for_virtual_instance(
    _isolated_log_overrides,
):
    """钉版本、默认调用目标与生效日志等级都要如实投影到分身实例的卡片上。"""
    set_plugin_instance_log_level("CatalogOverlayVirtual", "DEBUG")

    class DemoPlugin:
        plugin_name = "Demo"
        plugin_order = 0

    instance = PluginInstance(
        instance_id="CatalogOverlayVirtual",
        source_plugin_id="DemoPlugin",
        mode="virtual",
        plugin_version="1.2.0",
        follow_current_version=False,
        is_default_target=True,
    )
    facade = _facade(
        classes=lambda: {"CatalogOverlayVirtual": DemoPlugin},
        plugin_instance=lambda plugin_id: (
            instance if plugin_id == "CatalogOverlayVirtual" else None
        ),
        plugin_instances=lambda: {"CatalogOverlayVirtual": instance},
    )

    plugin = facade.local()[0]

    assert plugin.pinned_version == "1.2.0"
    assert plugin.is_default_target is True
    assert plugin.log_level_effective == "DEBUG"


def test_local_reports_no_pin_and_no_log_override_when_instance_follows_defaults(
    _isolated_log_overrides,
):
    """跟随当前版本、非默认目标、无日志覆盖的分身实例不应带任何叠加徽标信息。"""
    class DemoPlugin:
        plugin_name = "Demo"
        plugin_order = 0

    instance = PluginInstance(
        instance_id="CatalogOverlayDefaultVirtual",
        source_plugin_id="DemoPlugin",
        mode="virtual",
        plugin_version="1.2.0",
        follow_current_version=True,
        is_default_target=False,
    )
    facade = _facade(
        classes=lambda: {"CatalogOverlayDefaultVirtual": DemoPlugin},
        plugin_instance=lambda plugin_id: (
            instance if plugin_id == "CatalogOverlayDefaultVirtual" else None
        ),
        plugin_instances=lambda: {"CatalogOverlayDefaultVirtual": instance},
    )

    plugin = facade.local()[0]

    assert plugin.pinned_version is None
    assert plugin.is_default_target is False
    assert plugin.log_level_effective is None


def test_local_falls_back_to_host_binding_record_for_physical_plugin(
    _isolated_log_overrides,
):
    """物理插件没有分身记录时改用批量取到的本体绑定记录投影三个叠加字段。

    ``is_instance``、``instance_mode`` 只看分身记录，不受本体绑定记录影响，
    现有语义保持不变。
    """
    set_plugin_instance_log_level("CatalogOverlayHost", "WARNING")

    class DemoPlugin:
        plugin_name = "Demo"
        plugin_order = 0

    host_instance = PluginInstance(
        instance_id="CatalogOverlayHost",
        source_plugin_id="CatalogOverlayHost",
        mode="host",
        plugin_version="2.0.0",
        follow_current_version=False,
        is_default_target=True,
    )
    facade = _facade(
        classes=lambda: {"CatalogOverlayHost": DemoPlugin},
        host_instances=lambda: {"CatalogOverlayHost": host_instance},
    )

    plugin = facade.local()[0]

    assert plugin.is_instance is False
    assert plugin.instance_mode is None
    assert plugin.pinned_version == "2.0.0"
    assert plugin.is_default_target is True
    assert plugin.log_level_effective == "WARNING"


def test_local_defaults_overlay_fields_without_any_instance_record(
    _isolated_log_overrides,
):
    """既无分身也无本体绑定记录时，三个叠加字段要落到跟随全局的默认值。"""
    class DemoPlugin:
        plugin_name = "Demo"
        plugin_order = 0

    facade = _facade(classes=lambda: {"CatalogOverlayNone": DemoPlugin})

    plugin = facade.local()[0]

    assert plugin.pinned_version is None
    assert plugin.is_default_target is False
    assert plugin.log_level_effective is None


def test_installed_placeholder_projects_overlay_fields_from_host_binding_record(
    _isolated_log_overrides,
):
    """未加载插件的占位卡片同样要用批量取到的本体绑定记录投影叠加字段。"""
    host_instance = PluginInstance(
        instance_id="CatalogOverlayPlaceholder",
        source_plugin_id="CatalogOverlayPlaceholder",
        mode="host",
        plugin_version="0.9.0",
        follow_current_version=False,
        is_default_target=False,
    )
    facade = _facade(
        storage=lambda: SimpleNamespace(
            read=lambda key: (
                ["CatalogOverlayPlaceholder"]
                if key is SystemConfigKey.UserInstalledPlugins
                else None
            )
        ),
        host_instances=lambda: {"CatalogOverlayPlaceholder": host_instance},
    )

    plugin = facade.installed()[0]

    assert plugin.pinned_version == "0.9.0"
    assert plugin.is_default_target is False
    assert plugin.log_level_effective is None
