"""服务配置归属驱动的模块实例发现测试。"""

from typing import Dict, Iterator, List

import pytest

from app.modules import ServiceBase, _DownloaderBase
from app.runtime.extensions import service_config as service_config_module
from app.runtime.extensions import service_registry as service_registry_module
from app.runtime.extensions.host_module_adapter import build_host_module_registry
from app.runtime.extensions.service_config import ServiceConfigHelper
from app.runtime.extensions.service_registry import ServiceBaseHelper
from app.schemas.system import DownloaderConf, MediaServerConf, NotificationConf
from app.schemas.types import SystemConfigKey


# 三族模块在 manifest 中声明的服务配置归属
_EXPECTED_SERVICE_OWNERSHIP: Dict[str, set] = {
    SystemConfigKey.Downloaders.value: {
        "QbittorrentModule",
        "RtorrentModule",
        "TransmissionModule",
    },
    SystemConfigKey.MediaServers.value: {
        "EmbyModule",
        "JellyfinModule",
        "NavidromeModule",
        "PlexModule",
        "TrimeMediaModule",
        "UgreenModule",
        "ZSpaceModule",
    },
    SystemConfigKey.Notifications.value: {
        "DiscordModule",
        "FeishuModule",
        "QQBotModule",
        "SlackModule",
        "SynologyChatModule",
        "TelegramModule",
        "VoceChatModule",
        "WebPushModule",
        "WechatClawBotModule",
        "WechatModule",
    },
}


class _StubModule:
    """按名称持有若干服务实例的模块替身。"""

    def __init__(self, instances: Dict[str, object]) -> None:
        """记录该模块持有的服务实例。

        :param instances: 实例字典 ``{配置名称: 实例}``
        """
        self._instances = instances

    def get_instances(self) -> Dict[str, object]:
        """返回该模块持有的服务实例。

        :return: 实例字典 ``{配置名称: 实例}``
        """
        return self._instances


class _StubModuleManager:
    """只按服务配置归属回答查询的模块管理器替身。"""

    registry: Dict[str, List[_StubModule]] = {}

    def get_service_config_modules(self, config_key: str) -> Iterator[_StubModule]:
        """返回声明消费指定服务配置键的模块。

        :param config_key: 服务配置键
        :return: 模块迭代器
        """
        yield from self.registry.get(config_key, [])


@pytest.fixture
def service_configs(monkeypatch: pytest.MonkeyPatch) -> Iterator[Dict[str, list]]:
    """把服务配置读取和模块查找都重定向到内存替身。"""
    values: Dict[str, list] = {}

    def reader(config_key: SystemConfigKey):
        """按配置键返回内存中的原始配置。

        :param config_key: 服务配置键
        :return: 原始配置列表
        """
        return values.get(config_key.value)

    monkeypatch.setattr(_StubModuleManager, "registry", {})
    monkeypatch.setattr(
        service_registry_module,
        "ModuleManager",
        _StubModuleManager,
    )
    monkeypatch.setattr(service_config_module, "_service_config_reader", reader)
    yield values


def test_service_families_declare_config_ownership_in_manifest() -> None:
    """三族模块在 manifest 中声明消费的服务配置键，其余模块不声明。"""
    specs = build_host_module_registry().list_specs()

    ownership: Dict[str, set] = {}
    for spec in specs:
        service_config = spec.metadata.get("service_config")
        if service_config is None:
            continue
        ownership.setdefault(service_config, set()).add(spec.id)

    assert ownership == _EXPECTED_SERVICE_OWNERSHIP


def test_declared_service_config_matches_activation_selector() -> None:
    """声明服务配置归属的模块，其激活选择器读取的正是同一配置键。"""
    for spec in build_host_module_registry().list_specs():
        service_config = spec.metadata.get("service_config")
        if service_config is None:
            continue
        assert spec.selector is not None, spec.id
        assert spec.selector.config["key"] == service_config, spec.id
        assert service_config in spec.watch, spec.id


def test_modules_without_service_instances_declare_no_ownership() -> None:
    """不按服务配置扇出实例的模块不声明服务配置归属。"""
    declared = {
        spec.id
        for spec in build_host_module_registry().list_specs()
        if spec.metadata.get("service_config") is not None
    }

    assert "TheMovieDbModule" not in declared
    assert "LocalStorageModule" not in declared
    assert "IndexerModule" not in declared


@pytest.mark.parametrize(
    ("config_key", "conf_type", "raw_config"),
    [
        (
            SystemConfigKey.Downloaders,
            DownloaderConf,
            {"name": "客厅 下载器", "type": "qbittorrent", "enabled": True},
        ),
        (
            SystemConfigKey.MediaServers,
            MediaServerConf,
            {"name": "客厅 Emby", "type": "emby", "enabled": True},
        ),
        (
            SystemConfigKey.Notifications,
            NotificationConf,
            {"name": "家庭 群聊", "type": "wechat", "enabled": True},
        ),
    ],
)
def test_config_ownership_locates_each_family_instances(
    service_configs: Dict[str, list],
    config_key: SystemConfigKey,
    conf_type: type,
    raw_config: dict,
) -> None:
    """按配置归属可定位到各族模块持有的实例，且含中文与空格的服务名可直接用作实例标识。"""
    name = raw_config["name"]
    instance = object()
    service_configs[config_key.value] = [raw_config]
    _StubModuleManager.registry = {
        config_key.value: [_StubModule({name: instance})],
        "OtherKey": [_StubModule({"不该被取到": object()})],
    }

    helper = ServiceBaseHelper(config_key=config_key, conf_type=conf_type)
    services = helper.get_services()

    assert list(services) == [name]
    assert services[name].instance is instance
    assert services[name].type == raw_config["type"]
    assert services[name].config.name == name
    assert helper.get_service(name) is not None
    assert helper.get_service(name).instance is instance


def test_service_lookup_ignores_other_config_keys(
    service_configs: Dict[str, list],
) -> None:
    """消费其它服务配置键的模块不会被当前族取到。"""
    service_configs[SystemConfigKey.Downloaders.value] = [
        {"name": "下载器", "type": "qbittorrent", "enabled": True},
    ]
    _StubModuleManager.registry = {
        SystemConfigKey.MediaServers.value: [_StubModule({"下载器": object()})],
    }

    helper = ServiceBaseHelper(
        config_key=SystemConfigKey.Downloaders,
        conf_type=DownloaderConf,
    )

    assert helper.get_services() == {}


class _SampleService(ServiceBase[object, NotificationConf]):
    """按内存配置扇出实例的服务基类实现。"""

    def get_configs(self) -> Dict[str, NotificationConf]:
        """返回按服务名过滤后的已启用配置。

        :return: 配置字典 ``{配置名称: 配置}``
        """
        return {
            conf.name: conf
            for conf in ServiceConfigHelper.get_notification_configs()
            if conf.type == self._service_name and conf.enabled
        }


def test_default_instance_is_first_config(service_configs: Dict[str, list]) -> None:
    """未指定名称时取第一条配置对应的实例。"""
    service_configs[SystemConfigKey.Notifications.value] = [
        {"name": "家庭 群聊", "type": "wechat", "enabled": True},
        {"name": "运维告警", "type": "wechat", "enabled": True},
    ]
    service = _SampleService()
    service.init_service("wechat", service_type=lambda conf: conf.name)

    assert set(service.get_instances()) == {"家庭 群聊", "运维告警"}
    assert service.get_instance() == "家庭 群聊"
    assert service.get_instance(None) == "家庭 群聊"
    assert service.get_instance("运维告警") == "运维告警"
    assert service.get_instance("不存在") is None
    assert service.get_config().name == "家庭 群聊"


def test_default_instance_is_none_without_configs(
    service_configs: Dict[str, list],
) -> None:
    """没有任何配置时默认实例为空。"""
    service_configs[SystemConfigKey.Notifications.value] = []
    service = _SampleService()
    service.init_service("wechat", service_type=lambda conf: conf.name)

    assert service.get_instances() == {}
    assert service.get_instance() is None
    assert service.get_instance("家庭 群聊") is None


class _SampleDownloader(_DownloaderBase[object]):
    """按内存配置扇出实例的下载器基类实现。"""


def test_downloader_default_instance_prefers_default_flag(
    service_configs: Dict[str, list],
) -> None:
    """下载器未指定名称时优先取标记为默认的那一条配置。"""
    service_configs[SystemConfigKey.Downloaders.value] = [
        {"name": "客厅 下载器", "type": "qbittorrent", "enabled": True},
        {
            "name": "主力 下载器",
            "type": "qbittorrent",
            "enabled": True,
            "default": True,
        },
    ]
    service = _SampleDownloader()
    service.init_service("qbittorrent", service_type=lambda conf: conf.name)

    assert service.get_instance() == "主力 下载器"
    assert service.get_instance("客厅 下载器") == "客厅 下载器"
    assert service.get_config().name == "主力 下载器"
