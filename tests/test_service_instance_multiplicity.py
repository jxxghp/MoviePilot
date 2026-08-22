"""服务实例类型能配几份：声明表达实例数，而非按服务族硬编码。

判据见 docs/plugin-extension-architecture.md 第 7.4 节。缺省是多实例，因此不写
`multi_instance` 的声明与既有三族行为完全一致；写成 False 的类型至多产出一个实例，
配了多份时按默认调用目标裁决，裁决不出来就整个类型停摆。本文件覆盖声明缺省值、
契约校验、扇出裁剪、裁决与报错去重、登记项携带与端点下发。
"""

from typing import Any, Dict, Iterator, List, Optional

import pytest

from app.api.endpoints.service import config_form as service_config_form_endpoint
from app.application.downloader import DownloaderHelper
from app.foundation.singleton import Singleton
from app.runtime.extensions.registry import service_instance as registry_module
from app.runtime.extensions.contract.declaration import ServiceInstanceDeclaration
from app.runtime.extensions.projection.plugin import PluginProjection
from app.runtime.extensions.plugin_manager import PluginManager
from app.runtime.extensions.service_config import (
    configure_service_instance_config_reader,
)
from app.runtime.extensions.registry.service_instance import service_instance_registry
from app.schemas.service import ServiceConfigForm


class _DemoDownloader:
    """契约合规的下载器客户端桩，按 impl(name=..., **config) 构造。"""

    def __init__(self, name: Optional[str] = None, host: Optional[str] = None, **kwargs: Any):
        """记录宿主传入的实例名与配置内容。"""
        self.name = name
        self.host = host

    def is_inactive(self) -> bool:
        """回答连接是否已断开，宿主的十分钟重连回路直调它。"""
        return False

    def reconnect(self) -> bool:
        """重建连接，宿主判定失活后直调它。"""
        return True


class _RecordingLogger:
    """记录告警与错误文本的日志端口替身。"""

    def __init__(self):
        self.warnings: List[str] = []
        self.errors: List[str] = []

    def warning(self, message: str) -> None:
        """记录一条告警。"""
        self.warnings.append(message)

    def error(self, message: str) -> None:
        """记录一条错误。"""
        self.errors.append(message)

    def info(self, message: str) -> None:
        """记录一条信息，用例不关心其内容。"""


def _downloader_config(
    name: str, service_type: str, enabled: bool = True, default: bool = False, **config: Any
) -> dict:
    """构造一条下载器配置的原始字典。

    :param name: 实例名
    :param service_type: 类型标识
    :param enabled: 是否启用
    :param default: 是否为本族的默认调用目标
    :param config: 该实例的配置内容
    :return: 与持久化形状一致的配置字典
    """
    return {
        "name": name,
        "type": service_type,
        "enabled": enabled,
        "default": default,
        "config": config,
    }


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
def recording_logger(monkeypatch) -> _RecordingLogger:
    """接管注册表模块的日志端口，用例据此断言告警内容与条数。"""
    log = _RecordingLogger()
    monkeypatch.setattr(registry_module, "logger", log)
    return log


@pytest.fixture
def service_configs() -> Iterator[List[dict]]:
    """接管服务配置读取端口，用例改写列表即改写用户配置。"""
    configs: List[dict] = []
    previous = configure_service_instance_config_reader(
        lambda capability: configs if capability == "downloader" else None
    )
    try:
        yield configs
    finally:
        configure_service_instance_config_reader(previous)


@pytest.fixture
def plugin_manager() -> Iterator[PluginManager]:
    """构造隔离的插件管理器实例，避免单例状态污染其它用例。"""
    Singleton._instances.pop((PluginManager, (), frozenset()), None)
    manager = PluginManager()
    yield manager
    Singleton._instances.pop((PluginManager, (), frozenset()), None)


class _CapableServicePlugin:
    """声明服务实例类型的最小插件桩，用于直接驱动 PluginProjection。"""

    plugin_name = "服务插件"

    def __init__(self, declarations):
        self._declarations = declarations

    def get_state(self) -> bool:
        """返回插件启用状态。"""
        return True

    def get_render_mode(self):
        """返回插件渲染模式。"""
        return "vuetify", None

    def provides_service_instances(self):
        """返回声明的服务实例类型。"""
        return self._declarations


class _DownloaderTypePlugin:
    """声明一个下载器类型的插件桩，实例数由类属性决定。"""

    plugin_name = "多份下载器插件"
    plugin_version = "1.0.0"
    service_type = "demo_downloader"
    multi_instance = True

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

    def provides_service_instances(self) -> List[ServiceInstanceDeclaration]:
        """声明本插件提供的下载器类型。"""
        return [
            ServiceInstanceDeclaration(
                capability="downloader",
                type=self.service_type,
                name="演示下载器",
                impl=_DemoDownloader,
                multi_instance=self.multi_instance,
            )
        ]

    def close(self) -> None:
        """释放测试桩持有的资源，测试桩无资源可释放。"""

    def stop_service(self) -> None:
        """停止测试桩后台服务，测试桩无后台服务。"""


class _DefaultMultiplicityPlugin(_DownloaderTypePlugin):
    """不写 multi_instance 的插件桩，用于确认缺省行为与既有一致。"""

    plugin_name = "缺省下载器插件"
    service_type = "default_downloader"

    def provides_service_instances(self) -> List[ServiceInstanceDeclaration]:
        """声明的类型不带 multi_instance 字段。"""
        return [
            ServiceInstanceDeclaration(
                capability="downloader",
                type=self.service_type,
                name="缺省下载器",
                impl=_DemoDownloader,
            )
        ]


class _SingleInstancePlugin(_DownloaderTypePlugin):
    """声明只认一份配置的下载器类型的插件桩。"""

    plugin_name = "单份下载器插件"
    service_type = "single_downloader"
    multi_instance = False


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


def test_declaration_defaults_to_multi_instance():
    """不写该字段的声明默认为多实例，缺省值即既有三族的现行语义。"""
    declaration = ServiceInstanceDeclaration(
        capability="downloader", type="demo", name="演示", impl=_DemoDownloader
    )

    assert declaration.multi_instance is True


@pytest.mark.parametrize(
    "multi_instance",
    ["false", 0, 1, None, "true"],
    ids=["string_false", "int_zero", "int_one", "none", "string_true"],
)
def test_declaration_rejected_when_multiplicity_is_not_boolean(multi_instance):
    """multi_instance 非布尔时整条声明被拒，不悄悄按真值语义归一。"""
    plugin = _CapableServicePlugin([
        ServiceInstanceDeclaration(
            capability="downloader",
            type="demo",
            name="演示",
            impl=_DemoDownloader,
            multi_instance=multi_instance,
        )
    ])

    declared = PluginProjection({"DemoPlugin": plugin}).provided_service_instances()

    assert declared["DemoPlugin"] == []


@pytest.mark.parametrize("multi_instance", [True, False], ids=["multi", "single"])
def test_declaration_accepted_for_either_multiplicity(multi_instance):
    """两种取值都是合法声明，字段原样保留在声明里。"""
    declaration = ServiceInstanceDeclaration(
        capability="downloader",
        type="demo",
        name="演示",
        impl=_DemoDownloader,
        multi_instance=multi_instance,
    )

    declared = PluginProjection(
        {"DemoPlugin": _CapableServicePlugin([declaration])}
    ).provided_service_instances()

    assert declared["DemoPlugin"] == [declaration]
    assert declared["DemoPlugin"][0].multi_instance is multi_instance


@pytest.mark.parametrize(
    "plugin_class, service_type",
    [
        (_DefaultMultiplicityPlugin, "default_downloader"),
        (_DownloaderTypePlugin, "demo_downloader"),
    ],
    ids=["omitted", "explicit_true"],
)
def test_multi_instance_type_fans_out_every_config(
    monkeypatch, plugin_manager: PluginManager, service_configs: List[dict],
    plugin_class, service_type
):
    """多实例类型按配置逐条扇出，缺省与显式 True 的结果完全相同。"""
    service_configs.extend([
        _downloader_config("下载器甲", service_type, host="a"),
        _downloader_config("下载器乙", service_type, host="b"),
        _downloader_config("下载器丙", service_type, host="c"),
    ])
    _start_plugin(monkeypatch, plugin_manager, plugin_class)

    services = DownloaderHelper().get_services()

    assert sorted(services) == ["下载器丙", "下载器乙", "下载器甲"]
    assert services["下载器甲"].instance.host == "a"
    assert services["下载器丙"].instance.host == "c"


def test_single_instance_type_uses_the_default_target(
    monkeypatch, plugin_manager: PluginManager, service_configs: List[dict]
):
    """单实例类型配了多份时用显式的默认调用目标，与它排第几无关。"""
    service_configs.extend([
        _downloader_config("排在最前", "single_downloader", host="a"),
        _downloader_config("默认目标", "single_downloader", default=True, host="b"),
        _downloader_config("多余", "single_downloader", host="c"),
    ])
    _start_plugin(monkeypatch, plugin_manager, _SingleInstancePlugin)

    services = DownloaderHelper().get_services()

    assert list(services) == ["默认目标"]
    assert services["默认目标"].instance.host == "b"


def test_single_instance_type_produces_nothing_without_a_default_target(
    monkeypatch, plugin_manager: PluginManager, service_configs: List[dict],
    recording_logger: _RecordingLogger
):
    """裁决不出目标时整个类型不产出实例，绝不替用户挑一份跑起来。"""
    service_configs.extend([
        _downloader_config("甲", "single_downloader", host="a"),
        _downloader_config("乙", "single_downloader", host="b"),
    ])
    _start_plugin(monkeypatch, plugin_manager, _SingleInstancePlugin)

    services = DownloaderHelper().get_services()

    assert services == {}
    assert len(recording_logger.errors) == 1
    message = recording_logger.errors[0]
    assert "single_downloader" in message
    assert "甲" in message and "乙" in message


def test_single_instance_arbitration_failure_is_reported_once(
    monkeypatch, plugin_manager: PluginManager, service_configs: List[dict],
    recording_logger: _RecordingLogger
):
    """取服务是热路径，同一份裁决不出目标的配置反复取用也只报错一次。"""
    service_configs.extend([
        _downloader_config("甲", "single_downloader", host="a"),
        _downloader_config("乙", "single_downloader", host="b"),
    ])
    _start_plugin(monkeypatch, plugin_manager, _SingleInstancePlugin)

    for _ in range(5):
        DownloaderHelper().get_services()

    assert len(recording_logger.errors) == 1


@pytest.mark.parametrize(
    "plugin_class", [_DownloaderTypePlugin, _SingleInstancePlugin],
    ids=["multi", "single"],
)
def test_one_config_works_under_either_multiplicity(
    monkeypatch, plugin_manager: PluginManager, service_configs: List[dict], plugin_class
):
    """只配一份时两种取值行为一致，都产出那一个具名实例。"""
    service_configs.append(
        _downloader_config("唯一实例", plugin_class.service_type, host="a")
    )
    _start_plugin(monkeypatch, plugin_manager, plugin_class)

    services = DownloaderHelper().get_services()

    assert list(services) == ["唯一实例"]
    assert services["唯一实例"].instance.host == "a"


def test_disabled_configs_do_not_count_toward_the_single_slot(
    monkeypatch, plugin_manager: PluginManager, service_configs: List[dict],
    recording_logger: _RecordingLogger
):
    """停用的配置不占那唯一的名额：排在前面的被停用后由后一份接手，且不告警。"""
    service_configs.extend([
        _downloader_config("已停用", "single_downloader", enabled=False, host="a"),
        _downloader_config("生效的", "single_downloader", host="b"),
    ])
    _start_plugin(monkeypatch, plugin_manager, _SingleInstancePlugin)

    services = DownloaderHelper().get_services()

    assert list(services) == ["生效的"]
    assert recording_logger.warnings == []


def test_multi_instance_type_never_complains_about_the_count(
    monkeypatch, plugin_manager: PluginManager, service_configs: List[dict],
    recording_logger: _RecordingLogger
):
    """多实例类型配多少份都是正常用法，不得因份数告警或报错。"""
    service_configs.extend([
        _downloader_config("下载器甲", "demo_downloader", host="a"),
        _downloader_config("下载器乙", "demo_downloader", host="b"),
    ])
    _start_plugin(monkeypatch, plugin_manager, _DownloaderTypePlugin)

    DownloaderHelper().get_services()

    assert recording_logger.warnings == []
    assert recording_logger.errors == []


def test_arbitration_failure_is_reported_per_type_not_globally(
    service_configs: List[dict], recording_logger: _RecordingLogger
):
    """两个各自裁决不出目标的类型各报一次，互不掩盖。"""
    service_configs.extend([
        _downloader_config("甲一", "type_a", host="a"),
        _downloader_config("甲二", "type_a", host="b"),
        _downloader_config("乙一", "type_b", host="c"),
        _downloader_config("乙二", "type_b", host="d"),
    ])
    for service_type in ("type_a", "type_b"):
        service_instance_registry.register(
            capability="downloader",
            service_type=service_type,
            name=service_type,
            impl=_DemoDownloader,
            owner="DemoPlugin",
            multi_instance=False,
        )

    for adapter in service_instance_registry.adapters("downloader"):
        adapter.get_instances()

    assert len(recording_logger.errors) == 2


def test_registered_entry_carries_declared_multiplicity(
    monkeypatch, plugin_manager: PluginManager
):
    """声明的实例数必须原样进入登记项，不在中途丢失。"""
    _start_plugin(monkeypatch, plugin_manager, _SingleInstancePlugin)

    entry = service_instance_registry.find("downloader", "single_downloader")

    assert entry is not None
    assert entry.multi_instance is False


def test_registry_defaults_to_multi_instance_for_direct_registration():
    """直接登记不传该参数时缺省为多实例，与声明缺省口径一致。"""
    service_instance_registry.register(
        capability="downloader",
        service_type="direct_type",
        name="直接登记类型",
        impl=_DemoDownloader,
        owner="DemoPlugin",
    )

    assert service_instance_registry.find("downloader", "direct_type").multi_instance is True


@pytest.mark.parametrize("multi_instance", [True, False], ids=["multi", "single"])
def test_endpoint_reports_the_declared_multiplicity(multi_instance):
    """端点下发该字段，前端据此决定要不要给出新增第二份的入口。"""
    service_instance_registry.register(
        capability="downloader",
        service_type="demo_downloader",
        name="演示下载器",
        impl=_DemoDownloader,
        owner="DemoPlugin",
        multi_instance=multi_instance,
    )

    result = service_config_form_endpoint("downloader", "demo_downloader", None)

    assert result["multi_instance"] is multi_instance


def test_endpoint_answers_multi_instance_for_unregistered_type():
    """未登记的类型答可配多份，与内建类型一律多实例的现状一致。"""
    result = service_config_form_endpoint("downloader", "qbittorrent", None)

    assert result["multi_instance"] is True


def test_response_model_lets_the_multiplicity_through():
    """该字段必须在响应模型里，否则会被 FastAPI 静默裁掉，前端永远收到缺省值。"""
    service_instance_registry.register(
        capability="downloader",
        service_type="demo_downloader",
        name="演示下载器",
        impl=_DemoDownloader,
        owner="DemoPlugin",
        multi_instance=False,
    )

    payload = service_config_form_endpoint("downloader", "demo_downloader", None)
    serialized: Dict[str, Any] = ServiceConfigForm(**payload).model_dump()

    assert set(serialized) == set(payload)
    assert serialized["multi_instance"] is False
