"""服务配置归属驱动的模块实例发现测试。"""

from typing import Dict, Iterator, List

import pytest

from app.modules import ServiceBase, _DownloaderBase
from app.runtime.extensions import service_config as service_config_module
from app.runtime.extensions import service_registry as service_registry_module
from app.runtime.extensions.lifecycle.host_module_adapter import build_host_module_registry
from app.runtime.extensions.service_config import (
    ServiceConfigHelper,
    service_capability,
    service_config_key,
)
from app.runtime.extensions.registry.service_family import service_family_registry
from app.runtime.extensions.service_registry import ServiceBaseHelper
from app.schemas.system import DownloaderConf, MediaServerConf, NotificationConf
from app.schemas.types import ModuleType, SystemConfigKey


# 三族模块在 manifest 中声明的服务能力归属
_EXPECTED_SERVICE_OWNERSHIP: Dict[str, set] = {
    ModuleType.Downloader.value: {
        "QbittorrentModule",
        "RtorrentModule",
        "TransmissionModule",
    },
    ModuleType.MediaServer.value: {
        "EmbyModule",
        "JellyfinModule",
        "NavidromeModule",
        "PlexModule",
        "TrimeMediaModule",
        "UgreenModule",
        "ZSpaceModule",
    },
    ModuleType.Notification.value: {
        "DingTalkModule",
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
    def instance_reader(capability: str):
        """按能力标签返回内存中的原始配置。

        :param capability: 服务能力标签
        :return: 原始配置列表
        """
        config_key = service_config_key(capability)
        return values.get(config_key.value) if config_key else None

    monkeypatch.setattr(service_config_module, "_service_config_reader", reader)
    monkeypatch.setattr(
        service_config_module, "_service_instance_config_reader", instance_reader
    )
    yield values


def test_service_families_declare_capability_ownership_in_manifest() -> None:
    """三族模块在 manifest 中声明归属的服务能力标签，其余模块不声明。"""
    specs = build_host_module_registry().list_specs()

    ownership: Dict[str, set] = {}
    for spec in specs:
        capability = spec.metadata.get("service_capability")
        if capability is None:
            continue
        ownership.setdefault(capability, set()).add(spec.id)

    assert ownership == _EXPECTED_SERVICE_OWNERSHIP


def test_manifest_never_declares_storage_key_as_capability() -> None:
    """清单声明的是语义标签，`SystemConfigKey` 取值不得出现在该字段上。"""
    storage_keys = {member.value for member in SystemConfigKey}

    declared = {
        spec.metadata.get("service_capability")
        for spec in build_host_module_registry().list_specs()
    } - {None}

    assert declared
    assert declared.isdisjoint(storage_keys)


def test_declaration_vocabulary_matches_host_storage_mapping() -> None:
    """已登记服务族的能力标签与宿主内部的存放位置对照必须覆盖同一套取值。"""
    capabilities = service_family_registry.capabilities()
    mapped = {
        capability
        for capability in capabilities
        if service_config_key(capability) is not None
    }

    assert mapped == set(capabilities)
    assert {
        service_capability(service_config_key(capability).value)
        for capability in capabilities
    } == set(capabilities)


def test_declared_capability_matches_activation_selector() -> None:
    """声明服务归属的模块，其激活选择器读取的正是该族配置的存放位置。"""
    for spec in build_host_module_registry().list_specs():
        capability = spec.metadata.get("service_capability")
        if capability is None:
            continue
        config_key = service_config_key(capability)
        assert config_key is not None, spec.id
        assert spec.selector is not None, spec.id
        assert spec.selector.config["key"] == config_key.value, spec.id
        assert config_key.value in spec.watch, spec.id


def test_modules_without_service_instances_declare_no_ownership() -> None:
    """不按服务配置扇出实例的模块不声明服务能力归属。"""
    declared = {
        spec.id
        for spec in build_host_module_registry().list_specs()
        if spec.metadata.get("service_capability") is not None
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


def test_helper_accepts_config_key_value_as_string(
    service_configs: Dict[str, list],
) -> None:
    """配置键传取值字符串与传枚举成员等价，取服务不因入参写法而失败。"""
    instance = object()
    service_configs[SystemConfigKey.Downloaders.value] = [
        {"name": "下载器", "type": "qbittorrent", "enabled": True},
    ]
    _StubModuleManager.registry = {
        SystemConfigKey.Downloaders.value: [_StubModule({"下载器": instance})],
    }

    helper = ServiceBaseHelper(
        config_key=SystemConfigKey.Downloaders.value,
        conf_type=DownloaderConf,
    )

    assert helper.config_key is SystemConfigKey.Downloaders
    assert helper.get_services()["下载器"].instance is instance


def test_helper_rejects_unknown_config_key_at_construction() -> None:
    """取不到对应成员的配置键在构造时即被拒绝，不留到取服务时静默返回空。"""
    with pytest.raises(ValueError, match="NotARealConfigKey"):
        ServiceBaseHelper(config_key="NotARealConfigKey", conf_type=DownloaderConf)


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


def test_default_instance_requires_explicit_name_among_several(
    service_configs: Dict[str, list],
) -> None:
    """本族没有默认标记字段，多条已启用配置时不按顺序取第一条而是报错列候选。"""
    service_configs[SystemConfigKey.Notifications.value] = [
        {"name": "家庭 群聊", "type": "wechat", "enabled": True},
        {"name": "运维告警", "type": "wechat", "enabled": True},
    ]
    service = _SampleService()
    service.init_service("wechat", service_type=lambda conf: conf.name)

    assert set(service.get_instances()) == {"家庭 群聊", "运维告警"}
    assert service.get_instance("运维告警") == "运维告警"
    assert service.get_instance("不存在") is None

    with pytest.raises(LookupError) as excinfo:
        service.get_instance()
    message = str(excinfo.value)
    assert "家庭 群聊（已启用）" in message
    assert "运维告警（已启用）" in message

    with pytest.raises(LookupError):
        service.get_instance(None)
    with pytest.raises(LookupError):
        service.get_config()


def test_default_instance_is_the_sole_enabled_config(
    service_configs: Dict[str, list],
) -> None:
    """只有一条已启用配置时结果与登记顺序无关，可直接确定目标。"""
    service_configs[SystemConfigKey.Notifications.value] = [
        {"name": "家庭 群聊", "type": "wechat", "enabled": True},
        {"name": "运维告警", "type": "wechat", "enabled": False},
        {"name": "研发群", "type": "telegram", "enabled": True},
    ]
    service = _SampleService()
    service.init_service("wechat", service_type=lambda conf: conf.name)

    assert service.get_instance() == "家庭 群聊"
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


def test_downloader_default_ignores_disabled_default_flag(
    service_configs: Dict[str, list],
) -> None:
    """默认下载器已停用等同于没有默认，不改走另一个下载器而是报错列候选。"""
    service_configs[SystemConfigKey.Downloaders.value] = [
        {"name": "客厅 下载器", "type": "qbittorrent", "enabled": True},
        {
            "name": "主力 下载器",
            "type": "qbittorrent",
            "enabled": False,
            "default": True,
        },
    ]
    service = _SampleDownloader()
    service.init_service("qbittorrent", service_type=lambda conf: conf.name)

    with pytest.raises(LookupError) as excinfo:
        service.get_instance()
    message = str(excinfo.value)
    assert "主力 下载器" in message
    assert "客厅 下载器（已启用）" in message
    assert "主力 下载器（已停用）" in message
    assert service.get_instance("客厅 下载器") == "客厅 下载器"


def test_downloader_default_of_another_type_is_not_claimed(
    service_configs: Dict[str, list],
) -> None:
    """默认下载器属于别的类型时本模块没有默认，安静让开而不是缓存别人的名字。"""
    service_configs[SystemConfigKey.Downloaders.value] = [
        {"name": "客厅 下载器", "type": "qbittorrent", "enabled": True},
        {"name": "备用 下载器", "type": "qbittorrent", "enabled": True},
        {
            "name": "主力 下载器",
            "type": "transmission",
            "enabled": True,
            "default": True,
        },
    ]
    service = _SampleDownloader()
    service.init_service("qbittorrent", service_type=lambda conf: conf.name)

    assert service.get_default_config_name() is None
    assert service.get_instance() is None
    assert service.get_config() is None
    # 别的类型的名字不得被缓存下来，否则改判默认后仍会取到旧答案
    assert service._default_config_name is None

    owner = _SampleDownloader()
    owner.init_service("transmission", service_type=lambda conf: conf.name)
    assert owner.get_instance() == "主力 下载器"


def test_downloader_default_falls_back_to_the_sole_enabled_downloader(
    service_configs: Dict[str, list],
) -> None:
    """从未标记过默认且全部下载器只有一条已启用时，该条即目标。"""
    service_configs[SystemConfigKey.Downloaders.value] = [
        {"name": "客厅 下载器", "type": "qbittorrent", "enabled": True},
        {"name": "旧下载器", "type": "transmission", "enabled": False},
    ]
    service = _SampleDownloader()
    service.init_service("qbittorrent", service_type=lambda conf: conf.name)

    assert service.get_instance() == "客厅 下载器"

    other = _SampleDownloader()
    other.init_service("transmission", service_type=lambda conf: conf.name)
    assert other.get_default_config_name() is None


def test_downloader_without_default_flag_refuses_to_pick_among_several(
    service_configs: Dict[str, list],
) -> None:
    """从未标记过默认且有多条已启用下载器时报错，只有持有候选的模块出声。"""
    service_configs[SystemConfigKey.Downloaders.value] = [
        {"name": "客厅 下载器", "type": "qbittorrent", "enabled": True},
        {"name": "备用 下载器", "type": "transmission", "enabled": True},
    ]
    service = _SampleDownloader()
    service.init_service("qbittorrent", service_type=lambda conf: conf.name)

    with pytest.raises(LookupError) as excinfo:
        service.get_instance()
    message = str(excinfo.value)
    assert "客厅 下载器（已启用）" in message
    assert "备用 下载器（已启用）" in message

    idle = _SampleDownloader()
    idle.init_service("rtorrent", service_type=lambda conf: conf.name)
    assert idle.get_default_config_name() is None
