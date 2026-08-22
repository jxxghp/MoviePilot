"""服务实例类型专属配置界面：契约校验、登记表携带、端点下发与不串场断言。

设计判据见 docs/plugin-extension-architecture.md 第 4 节：每份配置界面归属声明它
的那条声明，不归属扩展本身。本文件覆盖服务实例族这一份界面，端点按「能力标签
加类型标识」两个维度下发。
"""

from typing import Any, Iterator, List, Optional

import pytest
from fastapi import HTTPException

from app.api.endpoints.service import config_form as service_config_form_endpoint
from app.foundation.singleton import Singleton
from app.runtime.extensions.contract.extension import ExtensionDistribution
from app.runtime.extensions.contract.declaration import ServiceInstanceDeclaration
from app.runtime.extensions.projection.plugin import PluginProjection
from app.runtime.extensions.plugin_manager import PluginManager
from app.runtime.extensions.registry.service_instance import service_instance_registry
from app.schemas.service import ServiceConfigForm

# 一份合法的配置界面：组件树加默认数据二元组，形状与 get_form() 相同
_VALID_CONFIG_FORM = (
    [{"component": "VTextField", "props": {"model": "host", "label": "地址"}}],
    {"host": ""},
)


class _DemoDownloader:
    """契约合规的下载器客户端桩。"""

    def __init__(self, name: Optional[str] = None, **kwargs: Any):
        """记录宿主传入的实例名。"""
        self.name = name

    def is_inactive(self) -> bool:
        """回答连接是否已断开，宿主的十分钟重连回路直调它。"""
        return False

    def reconnect(self) -> bool:
        """重建连接，宿主判定失活后直调它。"""
        return True


@pytest.fixture(autouse=True)
def _isolate_service_instance_registry() -> Iterator[None]:
    """快照并复原服务实例注册表，避免测试间相互污染。"""
    original = dict(service_instance_registry._adapters)
    try:
        yield
    finally:
        service_instance_registry._adapters.clear()
        service_instance_registry._adapters.update(original)


@pytest.fixture
def plugin_manager() -> Iterator[PluginManager]:
    """构造隔离的插件管理器实例，避免单例状态污染其它用例。"""
    Singleton._instances.pop((PluginManager, (), frozenset()), None)
    manager = PluginManager()
    yield manager
    Singleton._instances.pop((PluginManager, (), frozenset()), None)


def _start_plugin(monkeypatch, plugin_manager: PluginManager, plugin_class: type) -> str:
    """按插件类启动一个插件实例并返回其实例键。

    :param monkeypatch: pytest monkeypatch
    :param plugin_manager: 插件管理器
    :param plugin_class: 插件类
    :return: 实例键
    """
    plugin_id = plugin_class.__name__
    monkeypatch.setattr(
        plugin_manager,
        "_load_selective_plugins",
        lambda pid, installed, check: [plugin_class],
    )
    monkeypatch.setattr(plugin_manager, "get_plugin_config", lambda pid: {})
    plugin_manager.start(pid=plugin_id)
    return plugin_id


def test_endpoint_returns_declared_form_when_present():
    """声明带表单时，端点按配置键与类型返回该表单原样内容。"""
    service_instance_registry.register(
        capability="downloader",
        service_type="demo_downloader",
        name="演示下载器",
        impl=_DemoDownloader,
        owner="DemoPlugin@default",
        config_form=_VALID_CONFIG_FORM,
    )

    result = service_config_form_endpoint("downloader", "demo_downloader", None)

    assert result["available"] is True
    assert result["name"] == "演示下载器"
    assert result["conf"] == _VALID_CONFIG_FORM[0]
    assert result["model"] == _VALID_CONFIG_FORM[1]


def test_endpoint_reports_unavailable_when_declaration_has_no_form():
    """声明未带表单时，端点返回「无自带界面」而非报错，并仍给出展示名。"""
    service_instance_registry.register(
        capability="downloader",
        service_type="demo_downloader",
        name="演示下载器",
        impl=_DemoDownloader,
        owner="DemoPlugin@default",
    )

    result = service_config_form_endpoint("downloader", "demo_downloader", None)

    assert result["available"] is False
    assert result["name"] == "演示下载器"
    assert result["conf"] is None
    assert result["model"] is None
    assert result["component"] is None
    assert result["remote"] is None


def test_endpoint_reports_unavailable_for_builtin_type():
    """内建类型不在扩展登记表内，端点返回「无自带界面」，前端沿用内建渲染。"""
    result = service_config_form_endpoint("downloader", "qbittorrent", None)

    assert result == {
        "available": False, "name": None, "multi_instance": True, "conf": None,
        "model": None, "component": None, "remote": None, "config_schema": None,
    }


@pytest.mark.parametrize("capability", ["subtitleserver", "Downloaders"])
def test_endpoint_raises_404_for_capability_outside_service_families(capability):
    """不支持声明服务实例的能力标签视为请求出错，而非「无自带界面」。

    宿主存放这族配置的 systemconfig 列表名同样不是合法标签：端点的第一个维度是
    语义标签，不是存储位置。
    """
    with pytest.raises(HTTPException) as exc_info:
        service_config_form_endpoint(capability, "u115", None)

    assert exc_info.value.status_code == 404


def test_endpoint_isolates_same_type_across_capabilities():
    """同名类型登记在不同能力标签下时，端点各取各的界面，不串族。"""
    downloader_form = ([{"component": "VTextField"}], {"host": ""})
    notification_form = ([{"component": "VSwitch"}], {"token": ""})
    service_instance_registry.register(
        capability="downloader",
        service_type="same_name",
        name="下载器",
        impl=_DemoDownloader,
        owner="DemoPlugin@default",
        config_form=downloader_form,
    )
    service_instance_registry.register(
        capability="notification",
        service_type="same_name",
        name="通知渠道",
        impl=_DemoDownloader,
        owner="DemoPlugin@default",
        config_form=notification_form,
    )

    assert service_config_form_endpoint("downloader", "same_name", None)["conf"] == downloader_form[0]
    assert service_config_form_endpoint("notification", "same_name", None)["conf"] == notification_form[0]


def test_response_model_keeps_every_field_the_endpoint_returns():
    """端点返回的字段必须全部在响应模型里，否则会被 FastAPI 静默裁掉。"""
    service_instance_registry.register(
        capability="downloader",
        service_type="demo_downloader",
        name="演示下载器",
        impl=_DemoDownloader,
        owner="DemoPlugin@default",
        config_form=_VALID_CONFIG_FORM,
    )

    payload = service_config_form_endpoint("downloader", "demo_downloader", None)
    serialized = ServiceConfigForm(**payload).model_dump()

    assert set(serialized) == set(payload)
    assert serialized["available"] is True
    assert serialized["name"] == "演示下载器"
    assert serialized["conf"] == _VALID_CONFIG_FORM[0]
    assert serialized["model"] == _VALID_CONFIG_FORM[1]


class _Plugin:
    """声明服务实例类型的最小插件桩，用于直接驱动 PluginProjection。"""

    plugin_name = "服务插件"

    def __init__(self, declarations, render_mode=("vuetify", None)):
        self._declarations = declarations
        self._render_mode = render_mode

    def get_state(self) -> bool:
        """返回插件启用状态。"""
        return True

    def get_render_mode(self):
        """返回插件渲染模式。"""
        return self._render_mode

    def provides_service_instances(self):
        """返回声明的服务实例类型。"""
        return self._declarations


@pytest.mark.parametrize(
    "config_form, config_component, render_mode",
    [
        (("not-a-list", {"host": ""}), None, ("vuetify", None)),
        (([{"component": "VTextField"}], "not-a-dict"), None, ("vuetify", None)),
        (_VALID_CONFIG_FORM, "SomeConfig", ("vue", "dist/assets")),
        (None, "SomeConfig", ("vuetify", None)),
    ],
    ids=[
        "component_tree_not_list",
        "default_data_not_dict",
        "both_form_and_component",
        "vuetify_extension_declares_component",
    ],
)
def test_declaration_rejected_when_config_interface_is_malformed(
    config_form, config_component, render_mode
):
    """配置界面不合契约时整条服务实例声明被拒，不只是界面字段被忽略。"""
    plugin = _Plugin(
        [
            ServiceInstanceDeclaration(
                capability="downloader",
                type="bad_form_downloader",
                name="坏表单下载器",
                impl=_DemoDownloader,
                config_form=config_form,
                config_component=config_component,
            )
        ],
        render_mode=render_mode,
    )

    declared = PluginProjection({"BadFormPlugin": plugin}).provided_service_instances()

    assert declared["BadFormPlugin"] == []


class _VueServicePlugin:
    """vue 渲染模式下声明服务实例配置组件的插件桩。"""

    plugin_name = "Vue服务插件"
    plugin_version = "2.0.0"

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

    def provides_service_instances(self) -> List[ServiceInstanceDeclaration]:
        """声明本插件提供的下载器类型，附带该类型的 vue 模式配置组件名。"""
        return [
            ServiceInstanceDeclaration(
                capability="downloader",
                type="vue_mode_downloader",
                name="Vue下载器",
                impl=_DemoDownloader,
                config_component="MyDownloaderConfig",
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
    plugin_id = _start_plugin(monkeypatch, plugin_manager, _VueServicePlugin)

    result = service_config_form_endpoint("downloader", "vue_mode_downloader", None)

    assert result["available"] is True
    assert result["conf"] is None
    assert result["model"] is None
    assert result["component"] == "MyDownloaderConfig"
    assert result["remote"]["id"] == plugin_id
    assert result["remote"]["name"] == _VueServicePlugin.plugin_name
    assert result["remote"]["version"] == _VueServicePlugin.plugin_version
    assert result["remote"]["remote_key"] == f"{plugin_id}#{_VueServicePlugin.plugin_version}"

    serialized = ServiceConfigForm(**result).model_dump()
    assert set(serialized["remote"]) == set(result["remote"])

    plugin_manager.stop(plugin_id)


class _MultiCapabilityPlugin:
    """同时提供服务实例类型与自身设置页的插件桩，用于验证两份界面互不串场。

    ``get_form()`` 是扩展自身的设置页；``provides_service_instances()`` 携带的
    ``config_form`` 是该服务类型的专属界面。两者形状刻意不同，混淆即可被断言捕获。
    """

    plugin_name = "多能力插件"
    plugin_version = "1.0.0"

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
        """返回插件自身设置页表单，形状与服务实例表单刻意不同。"""
        return (
            [{"component": "VSwitch", "props": {"model": "enable", "label": "启用"}}],
            {"enable": True},
        )

    def provides_service_instances(self):
        """声明本插件提供的下载器类型，附带该类型的专属配置界面。"""
        return [
            ServiceInstanceDeclaration(
                capability="downloader",
                type="multi_capability_downloader",
                name="多能力下载器",
                impl=_DemoDownloader,
                config_form=_VALID_CONFIG_FORM,
            )
        ]

    def close(self) -> None:
        """释放测试桩持有的资源，测试桩无资源可释放。"""

    def stop_service(self) -> None:
        """停止测试桩后台服务，测试桩无后台服务。"""


def test_service_form_does_not_leak_plugin_own_settings_form(
    monkeypatch, plugin_manager: PluginManager
):
    """插件同时声明服务实例与自身设置页时，取服务表单只拿到服务那份声明的表单。"""
    plugin_id = _start_plugin(monkeypatch, plugin_manager, _MultiCapabilityPlugin)

    result = service_config_form_endpoint("downloader", "multi_capability_downloader", None)

    assert result["available"] is True
    assert result["conf"] == _VALID_CONFIG_FORM[0]
    assert result["model"] == _VALID_CONFIG_FORM[1]

    own_form_conf, own_form_model = plugin_manager._running_plugins[plugin_id].get_form()
    assert result["conf"] != own_form_conf
    assert result["model"] != own_form_model

    plugin_manager.stop(plugin_id)


def test_endpoint_no_longer_returns_form_after_extension_stopped(
    monkeypatch, plugin_manager: PluginManager
):
    """扩展停用后其服务类型不再登记，端点不再能取得该扩展声明的表单。"""
    plugin_id = _start_plugin(monkeypatch, plugin_manager, _MultiCapabilityPlugin)
    assert service_config_form_endpoint(
        "downloader", "multi_capability_downloader", None
    )["available"] is True

    plugin_manager.stop(plugin_id)

    result = service_config_form_endpoint("downloader", "multi_capability_downloader", None)
    assert result["available"] is False
    assert result["name"] is None


def test_registered_entry_carries_declared_form(monkeypatch, plugin_manager: PluginManager):
    """声明携带的表单必须原样进入登记项，不在中途丢失。"""
    plugin_id = _start_plugin(monkeypatch, plugin_manager, _MultiCapabilityPlugin)

    entry = service_instance_registry.find("downloader", "multi_capability_downloader")
    assert entry.config_form == _VALID_CONFIG_FORM
    assert entry.config_component is None
    assert entry.distribution == ExtensionDistribution.MARKET
    assert entry.owner == plugin_id

    plugin_manager.stop(plugin_id)
