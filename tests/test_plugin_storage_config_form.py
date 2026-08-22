"""存储类型专属配置界面：契约校验、登记表携带、端点下发与不串场断言。

设计判据见 docs/plugin-extension-architecture.md 第 4 节：每份配置界面归属声明它
的那条声明，不归属扩展本身；扩展声明 N 种能力即有 N+1 份界面，各自只在自己的
场景出现。本文件覆盖存储这一份界面在存储器族上的落地。
"""

from typing import Iterator

import pytest
from fastapi import HTTPException

from app.api.endpoints.storage import config_form as storage_config_form_endpoint
from app.foundation.singleton import Singleton
from app.runtime.extensions.contract.extension import ExtensionDistribution
from app.runtime.extensions.contract.declaration import ServiceInstanceDeclaration
from app.runtime.extensions.projection.plugin import PluginProjection
from app.runtime.extensions.plugin_manager import PluginManager
from app.runtime.extensions.registry.storage import storage_backend_registry
from tests.test_plugin_provided_storages import (
    _storage_declaration,
    _ValidPluginStorage,
)

# 一份合法的配置界面：组件树加默认数据二元组，形状与 get_form() 相同
_VALID_CONFIG_FORM = (
    [{"component": "VTextField", "props": {"model": "token", "label": "Token"}}],
    {"token": ""},
)


@pytest.fixture(autouse=True)
def _isolate_storage_registry() -> Iterator[None]:
    """快照并复原存储后端注册表，避免测试间相互污染。"""
    original_entries = dict(storage_backend_registry._entries)
    original_builtin = dict(storage_backend_registry._builtin_entries)
    try:
        yield
    finally:
        storage_backend_registry._entries.clear()
        storage_backend_registry._entries.update(original_entries)
        storage_backend_registry._builtin_entries.clear()
        storage_backend_registry._builtin_entries.update(original_builtin)


@pytest.fixture
def plugin_manager() -> Iterator[PluginManager]:
    """构造隔离的插件管理器实例，避免单例状态污染其它用例。"""
    Singleton._instances.pop((PluginManager, (), frozenset()), None)
    manager = PluginManager()
    yield manager
    Singleton._instances.pop((PluginManager, (), frozenset()), None)


def test_endpoint_returns_declared_form_when_present():
    """声明带表单时，端点按存储类型返回该表单原样内容。"""
    storage_backend_registry.register(
        _ValidPluginStorage,
        distribution=ExtensionDistribution.MARKET,
        owner="DemoPlugin@default",
        storage_id="demo_storage",
        config_form=_VALID_CONFIG_FORM,
    )

    result = storage_config_form_endpoint("demo_storage", None)

    assert result["available"] is True
    assert result["conf"] == _VALID_CONFIG_FORM[0]
    assert result["model"] == _VALID_CONFIG_FORM[1]


def test_endpoint_reports_unavailable_when_declaration_has_no_form():
    """声明未带表单时，端点返回「无自带界面」而非报错。"""
    storage_backend_registry.register(
        _ValidPluginStorage,
        distribution=ExtensionDistribution.MARKET,
        owner="DemoPlugin@default",
        storage_id="demo_storage",
    )

    result = storage_config_form_endpoint("demo_storage", None)

    assert result == {
        "available": False, "conf": None, "model": None, "component": None, "remote": None,
    }


def test_endpoint_reports_unavailable_for_builtin_type():
    """内建类型没有随登记附带表单，端点返回「无自带界面」，前端沿用内建渲染。"""
    storage_backend_registry.register(
        _ValidPluginStorage,
        distribution=ExtensionDistribution.BUILTIN,
        owner="LocalStorageModule",
        storage_id="local",
    )

    result = storage_config_form_endpoint("local", None)

    assert result == {
        "available": False, "conf": None, "model": None, "component": None, "remote": None,
    }


def test_endpoint_raises_404_for_unknown_storage_type():
    """存储标识本身未登记时，端点视为请求出错而非「无自带界面」。"""
    with pytest.raises(HTTPException) as exc_info:
        storage_config_form_endpoint("never_registered_storage", None)

    assert exc_info.value.status_code == 404


def test_declaration_rejected_when_component_tree_is_not_list():
    """config_form 的组件树不是 list 时，整条存储声明被拒，不只是表单字段被忽略。"""

    class _Plugin:
        plugin_name = "存储插件"

        def get_state(self) -> bool:
            return True

        def provides_service_instances(self):
            return [
                _storage_declaration(
                    "bad_layout_storage",
                    impl=_ValidPluginStorage,
                    config_form=("not-a-list", {"token": ""}),
                )
            ]

    projection = PluginProjection({"BadLayoutPlugin": _Plugin()})

    declared = projection.provided_service_instances()

    assert declared["BadLayoutPlugin"] == []


def test_declaration_rejected_when_default_data_is_not_dict():
    """config_form 的默认数据不是 dict 时，整条存储声明被拒。"""

    class _Plugin:
        plugin_name = "存储插件"

        def get_state(self) -> bool:
            return True

        def provides_service_instances(self):
            return [
                _storage_declaration(
                    "bad_defaults_storage",
                    impl=_ValidPluginStorage,
                    config_form=([{"component": "VTextField"}], "not-a-dict"),
                )
            ]

    projection = PluginProjection({"BadDefaultsPlugin": _Plugin()})

    declared = projection.provided_service_instances()

    assert declared["BadDefaultsPlugin"] == []


def test_declaration_rejected_when_both_config_form_and_config_component_given():
    """config_form 与 config_component 同时声明时意图不明，整条声明被拒。"""

    class _Plugin:
        plugin_name = "存储插件"

        def get_state(self) -> bool:
            return True

        def get_render_mode(self):
            return "vue", "dist/assets"

        def provides_service_instances(self):
            return [
                _storage_declaration(
                    "both_given_storage",
                    impl=_ValidPluginStorage,
                    config_form=_VALID_CONFIG_FORM,
                    config_component="SomeConfig",
                )
            ]

    projection = PluginProjection({"BothGivenPlugin": _Plugin()})

    declared = projection.provided_service_instances()

    assert declared["BothGivenPlugin"] == []


def test_declaration_rejected_when_vuetify_extension_declares_config_component():
    """渲染模式为 vuetify 的扩展声明 config_component 属于矛盾声明，被拒。"""

    class _Plugin:
        plugin_name = "存储插件"

        def get_state(self) -> bool:
            return True

        def get_render_mode(self):
            return "vuetify", None

        def provides_service_instances(self):
            return [
                _storage_declaration(
                    "vuetify_declares_component_storage",
                    impl=_ValidPluginStorage,
                    config_component="SomeConfig",
                )
            ]

    projection = PluginProjection({"VuetifyDeclaresComponentPlugin": _Plugin()})

    declared = projection.provided_service_instances()

    assert declared["VuetifyDeclaresComponentPlugin"] == []


class _VueStoragePlugin:
    """vue 渲染模式下声明存储配置组件的插件桩。"""

    plugin_name = "Vue存储插件"
    plugin_version = "2.0.0"
    storage_schema = "vue_mode_storage"
    config_component_name = "U115StorageConfig"

    def __init__(self):
        self.enabled = True

    def init_plugin(self, config: dict = None) -> None:
        """生效配置信息，测试桩不使用配置内容。"""

    def get_state(self) -> bool:
        """返回插件启用状态。"""
        return self.enabled

    def get_name(self) -> str:
        """返回插件名称。"""
        return self.plugin_name

    def get_render_mode(self):
        """声明本插件按 vue 模式渲染，编译产物位于 dist/assets。"""
        return "vue", "dist/assets"

    def provides_service_instances(self):
        """声明本插件提供的存储后端，附带该存储类型的 vue 模式配置组件名。"""
        return [
            _storage_declaration(
                self.storage_schema,
                impl=_ValidPluginStorage,
                config_component=self.config_component_name,
            )
        ]

    def close(self) -> None:
        """释放测试桩持有的资源，测试桩无资源可释放。"""

    def stop_service(self) -> None:
        """停止测试桩后台服务，测试桩无后台服务。"""


def test_endpoint_returns_component_and_remote_for_vue_mode_declaration(
    monkeypatch, plugin_manager: PluginManager
):
    """vue 模式声明登记后，端点返回组件名与联邦远程入口描述，不返回 vuetify 字段。"""
    plugin_id = _VueStoragePlugin.__name__
    monkeypatch.setattr(
        plugin_manager,
        "_load_selective_plugins",
        lambda pid, installed, check: [_VueStoragePlugin],
    )
    monkeypatch.setattr(plugin_manager, "get_plugin_config", lambda pid: {})

    plugin_manager.start(pid=plugin_id)

    result = storage_config_form_endpoint("vue_mode_storage", None)

    assert result["available"] is True
    assert result["conf"] is None
    assert result["model"] is None
    assert result["component"] == "U115StorageConfig"
    assert result["remote"] is not None
    assert result["remote"]["id"] == plugin_id
    assert result["remote"]["name"] == _VueStoragePlugin.plugin_name
    assert result["remote"]["version"] == _VueStoragePlugin.plugin_version
    assert result["remote"]["remote_key"] == f"{plugin_id}#{_VueStoragePlugin.plugin_version}"

    plugin_manager.stop(plugin_id)


class _MultiCapabilityPlugin:
    """同时提供存储后端与自身设置页的插件桩，用于验证两份界面互不串场。

    ``get_form()`` 是扩展自身的设置页；``provides_service_instances()`` 携带的
    ``config_form`` 是该存储类型的专属界面。两者形状刻意不同，混淆即可被
    断言捕获。
    """

    plugin_name = "多能力插件"
    plugin_version = "1.0.0"
    storage_schema = "multi_capability_storage"

    def __init__(self):
        self.enabled = True

    def init_plugin(self, config: dict = None) -> None:
        """生效配置信息，测试桩不使用配置内容。"""

    def get_state(self) -> bool:
        """返回插件启用状态。"""
        return self.enabled

    def get_name(self) -> str:
        """返回插件名称。"""
        return self.plugin_name

    def get_form(self):
        """返回插件自身设置页表单，形状与存储表单刻意不同。"""
        return (
            [{"component": "VSwitch", "props": {"model": "enable", "label": "启用"}}],
            {"enable": True},
        )

    def provides_service_instances(self):
        """声明本插件提供的存储后端，附带该存储类型的专属配置界面。"""
        return [
            _storage_declaration(
                self.storage_schema,
                impl=_ValidPluginStorage,
                config_form=_VALID_CONFIG_FORM,
            )
        ]

    def close(self) -> None:
        """释放测试桩持有的资源，测试桩无资源可释放。"""

    def stop_service(self) -> None:
        """停止测试桩后台服务，测试桩无后台服务。"""


def test_storage_form_does_not_leak_plugin_own_settings_form(
    monkeypatch, plugin_manager: PluginManager
):
    """插件同时声明存储与自身设置页时，取存储表单只拿到存储那份声明的表单。"""
    plugin_id = _MultiCapabilityPlugin.__name__
    monkeypatch.setattr(
        plugin_manager,
        "_load_selective_plugins",
        lambda pid, installed, check: [_MultiCapabilityPlugin],
    )
    monkeypatch.setattr(plugin_manager, "get_plugin_config", lambda pid: {})

    plugin_manager.start(pid=plugin_id)

    result = storage_config_form_endpoint("multi_capability_storage", None)

    assert result["available"] is True
    assert result["conf"] == _VALID_CONFIG_FORM[0]
    assert result["model"] == _VALID_CONFIG_FORM[1]

    plugin_instance = plugin_manager._running_plugins[plugin_id]
    own_form_conf, own_form_model = plugin_instance.get_form()
    assert result["conf"] != own_form_conf
    assert result["model"] != own_form_model

    plugin_manager.stop(plugin_id)


def test_endpoint_no_longer_returns_form_after_extension_disabled(
    monkeypatch, plugin_manager: PluginManager
):
    """扩展停用后其存储标识不再登记，端点不再能取得该扩展声明的表单。"""
    plugin_id = _MultiCapabilityPlugin.__name__
    monkeypatch.setattr(
        plugin_manager,
        "_load_selective_plugins",
        lambda pid, installed, check: [_MultiCapabilityPlugin],
    )
    monkeypatch.setattr(plugin_manager, "get_plugin_config", lambda pid: {})

    plugin_manager.start(pid=plugin_id)
    result = storage_config_form_endpoint("multi_capability_storage", None)
    assert result["available"] is True

    plugin_manager.stop(plugin_id)

    with pytest.raises(HTTPException) as exc_info:
        storage_config_form_endpoint("multi_capability_storage", None)
    assert exc_info.value.status_code == 404
