"""三族实例配置读写从 systemconfig 切到服务实例配置表。

切表要证的第一件事是**用户看不出差别**：同一份配置数据，走切表前的读取链路和走表的
读取链路，扇出的实例必须逐条相同——实例名、类型、启用与否、类型实现拿到的配置内容、
以及宿主自己消费的实例级字段。这条对拍是整笔改动的兜底，其余用例都是它的分解。

写入侧证两件事：整族写入前仍按各类型声明的契约逐条判定（一条不合即退回整次写入），
以及写进表后再读出来还是同一份配置。
"""
from typing import Any, Dict, List

import pytest

from app.application.service_config import ServiceInstanceConfigService
from app.db.models.serviceconfig import BUILTIN_PROVIDER, ServiceConfig
from app.db.oper.serviceconfig import ServiceConfigOper
from app.runtime.extensions import service_config as service_config_module
from app.runtime.extensions.service_config import (
    ServiceConfigHelper,
    configure_service_instance_config_reader,
    create_service_instance,
    select_instance_configs,
    service_capability_configs,
    service_host_fields,
)
from app.runtime.extensions.admission.service_config import (
    service_config_records,
    service_config_write_violation,
)
from app.runtime.extensions.registry.service_instance import service_instance_registry
from app.schemas.system import DownloaderConf, MediaServerConf, NotificationConf
from app.schemas.types import MessageType, ModuleType


# 一份覆盖三族的存量配置：含宿主消费字段、默认标记、停用条目与多类型
_STORED: Dict[str, List[dict]] = {
    ModuleType.Downloader.value: [
        {
            "name": "qb 主力", "type": "qbittorrent", "enabled": True, "default": True,
            "config": {"host": "127.0.0.1", "port": 8080},
            "path_mapping": [["/media", "/downloads"]],
        },
        {
            "name": "tr 备用", "type": "transmission", "enabled": True,
            "config": {"host": "10.0.0.2"},
        },
        {
            "name": "已停用", "type": "qbittorrent", "enabled": False,
            "config": {"host": "10.0.0.3"},
        },
    ],
    ModuleType.MediaServer.value: [
        {
            "name": "emby 家用", "type": "emby", "enabled": True,
            "config": {"host": "emby.local", "apikey": "k"},
            "sync_libraries": ["1", "3"], "sync_interval": 12,
        },
        {
            "name": "plex", "type": "plex", "enabled": True,
            "config": {"token": "t"}, "sync_libraries": ["all"],
        },
    ],
    ModuleType.Notification.value: [
        {
            "name": "电报", "type": "telegram", "enabled": True,
            "config": {"token": "tg"}, "switchs": ["Manual", "SubscribeAdded"],
        },
        {
            "name": "企微", "type": "wechat", "enabled": False,
            "config": {"corpid": "c"}, "switchs": [],
        },
    ],
}

_CONF_TYPES = {
    ModuleType.Downloader.value: DownloaderConf,
    ModuleType.MediaServer.value: MediaServerConf,
    ModuleType.Notification.value: NotificationConf,
}


class _Client:
    """按 ``impl(name=..., **config)`` 构造的服务客户端桩。"""

    def __init__(self, name: str = None, **config: Any):
        """记录宿主填入的实例名与展开的配置内容。"""
        self.name = name
        self.config = config


@pytest.fixture(autouse=True)
def _track(db):
    """把服务实例配置表纳入用例级回收。"""
    db.watermark(ServiceConfig)


@pytest.fixture
def instance_configs(db) -> ServiceInstanceConfigService:
    """返回接在真实数据库上的服务实例配置服务。"""
    return ServiceInstanceConfigService(repository=ServiceConfigOper(db=db.session))


def _fanout(configs: List[Any], service_type: str) -> Dict[str, dict]:
    """按一个类型扇出实例，并把结果摊成可比对的形状。

    :param configs: 该族全部配置对象
    :param service_type: 类型标识
    :return: 实例名到「类型、实例拿到的配置内容、宿主消费字段」的映射
    """
    capability = next(
        cap for cap, conf_type in _CONF_TYPES.items()
        if configs and isinstance(configs[0], conf_type)
    )
    selected = select_instance_configs(configs, service_type)
    result = {}
    for name, conf in selected.items():
        instance = create_service_instance(name, conf, impl=_Client)
        result[name] = {
            "type": conf.type,
            "config": instance.config,
            "host": {
                field: getattr(conf, field) for field in service_host_fields(capability)
            },
        }
    return result


def _fanout_family(configs: List[Any]) -> Dict[str, Dict[str, dict]]:
    """把一族配置按其中出现的每个类型逐个扇出。

    :param configs: 该族全部配置对象
    :return: 类型标识到扇出结果的映射
    """
    types = sorted({conf.type for conf in configs if conf.type})
    return {service_type: _fanout(configs, service_type) for service_type in types}


def _before_switch(capability: str) -> Dict[str, Dict[str, dict]]:
    """按切表前的链路扇出：原始列表直接喂给族配置模型。

    :param capability: 能力标签
    :return: 扇出结果
    """
    conf_type = _CONF_TYPES[capability]
    return _fanout_family([conf_type(**conf) for conf in _STORED[capability]])


def _after_switch(capability: str, service: ServiceInstanceConfigService) -> Dict[str, Dict[str, dict]]:
    """按切表后的链路扇出：整族写进表，再从表读回来。

    :param capability: 能力标签
    :param service: 服务实例配置服务
    :return: 扇出结果
    """
    service.save(capability, _STORED[capability])
    previous = configure_service_instance_config_reader(service.read)
    try:
        return _fanout_family(service_capability_configs(capability))
    finally:
        configure_service_instance_config_reader(previous)


@pytest.mark.parametrize(
    "capability",
    [
        ModuleType.Downloader.value,
        ModuleType.MediaServer.value,
        ModuleType.Notification.value,
    ],
    ids=["downloader", "mediaserver", "notification"],
)
def test_fanout_is_identical_before_and_after_the_switch(
    capability: str, instance_configs: ServiceInstanceConfigService
):
    """同一份配置数据，切表前后扇出的实例逐条相同。"""
    before = _before_switch(capability)
    after = _after_switch(capability, instance_configs)

    assert after == before


def test_default_mark_survives_the_round_trip(instance_configs: ServiceInstanceConfigService):
    """默认标记进表变成 ``is_default_target``，读出来仍在配置模型顶层。"""
    instance_configs.save(ModuleType.Downloader.value, _STORED[ModuleType.Downloader.value])

    payloads = {item["name"]: item for item in instance_configs.read(ModuleType.Downloader.value)}
    assert payloads["qb 主力"]["default"] is True
    assert payloads["tr 备用"]["default"] is False


def test_only_one_default_target_survives_a_write_with_several(
    instance_configs: ServiceInstanceConfigService, db
):
    """写入时多条 default 为真只留第一条，否则直接撞上条件唯一索引。"""
    instance_configs.save(ModuleType.Notification.value, [
        {"name": "甲", "type": "telegram", "enabled": True, "default": True},
        {"name": "乙", "type": "slack", "enabled": True, "default": True},
    ])

    rows = ServiceConfigOper(db=db.session).list_by_capability(ModuleType.Notification.value)
    assert [row.name for row in rows if row.is_default_target] == ["甲"]


def test_write_drops_entries_without_an_identity(
    instance_configs: ServiceInstanceConfigService
):
    """写入时丢弃取不到名称或类型的条目：表按身份三元组定位一行，装不下它们。"""
    instance_configs.save(ModuleType.Downloader.value, [
        {"type": "qbittorrent", "enabled": True},
        {"name": "有身份", "type": "qbittorrent", "enabled": True},
        {"name": " ", "type": "qbittorrent"},
    ])

    payloads = instance_configs.read(ModuleType.Downloader.value)
    assert [item["name"] for item in payloads] == ["有身份"]


def test_write_replaces_the_whole_family(instance_configs: ServiceInstanceConfigService):
    """整族写入即整族覆盖：这一次没交出来的实例要从表里消失。"""
    instance_configs.save(ModuleType.Downloader.value, [
        {"name": "甲", "type": "qbittorrent", "enabled": True},
        {"name": "乙", "type": "transmission", "enabled": True},
    ])

    instance_configs.save(ModuleType.Downloader.value, [
        {"name": "乙", "type": "transmission", "enabled": False},
    ])

    payloads = instance_configs.read(ModuleType.Downloader.value)
    assert [item["name"] for item in payloads] == ["乙"]
    assert payloads[0]["enabled"] is False


def test_write_reports_whether_anything_changed(
    instance_configs: ServiceInstanceConfigService
):
    """内容没变时返回 False：配置变更事件会触发整族模块重载，不该被空保存触发。"""
    value = [{"name": "甲", "type": "qbittorrent", "enabled": True, "config": {"host": "h"}}]

    assert instance_configs.save(ModuleType.Downloader.value, value) is True
    assert instance_configs.save(ModuleType.Downloader.value, value) is False


def test_write_keeps_the_row_provider_when_the_type_is_unknown(
    instance_configs: ServiceInstanceConfigService, db
):
    """
    提供该类型的扩展当前不在场时，保存不得把 provider 抹成内建。

    抹掉之后「该类型由扩展 X 提供，X 当前未启用」这条提示就再也筛不出这一行，而这正是
    加这一列的目的。
    """
    ServiceConfigOper(db=db.session).add(
        ModuleType.Downloader.value, "custom", "插件下载器", provider="SomePlugin"
    )

    instance_configs.save(ModuleType.Downloader.value, [
        {"name": "插件下载器", "type": "custom", "enabled": True},
    ])

    row = ServiceConfigOper(db=db.session).get(
        ModuleType.Downloader.value, "custom", "插件下载器"
    )
    assert row.provider == "SomePlugin"


def test_write_backfills_the_provider_from_the_registry(
    instance_configs: ServiceInstanceConfigService, db
):
    """登记表当下知道该类型归谁时，新写入的行按登记回填提供方。"""
    service_instance_registry.register(
        capability=ModuleType.Downloader.value,
        service_type="registered_type",
        name="已登记类型",
        owner="OwnerPlugin",
        impl=_Client,
    )
    try:
        instance_configs.save(ModuleType.Downloader.value, [
            {"name": "新实例", "type": "registered_type", "enabled": True},
        ])
    finally:
        service_instance_registry.unregister_owner("OwnerPlugin")

    row = ServiceConfigOper(db=db.session).get(
        ModuleType.Downloader.value, "registered_type", "新实例"
    )
    assert row.provider == "OwnerPlugin"


def test_write_falls_back_to_the_builtin_provider(
    instance_configs: ServiceInstanceConfigService, db
):
    """登记表查不到又没有旧行时落到内建保留值，而不是留空。"""
    instance_configs.save(ModuleType.Downloader.value, [
        {"name": "内建实例", "type": "qbittorrent", "enabled": True},
    ])

    row = ServiceConfigOper(db=db.session).get(
        ModuleType.Downloader.value, "qbittorrent", "内建实例"
    )
    assert row.provider == BUILTIN_PROVIDER


def test_write_path_still_rejects_a_config_violating_its_type_contract():
    """整形之前仍按各类型声明的契约逐条判定，一条不合即退回整次写入。"""
    service_instance_registry.register(
        capability=ModuleType.Downloader.value,
        service_type="strict_type",
        name="严格类型",
        owner="OwnerPlugin",
        impl=_Client,
        config_schema={
            "type": "object",
            "properties": {"host": {"type": "string"}},
            "required": ["host"],
            "additionalProperties": False,
        },
    )
    try:
        violation = service_config_write_violation(ModuleType.Downloader.value, [
            {"name": "合规的", "type": "strict_type", "config": {"host": "h"}},
            {"name": "坏的", "type": "strict_type", "config": {"port": 1}},
        ])
    finally:
        service_instance_registry.unregister_owner("OwnerPlugin")

    assert violation is not None
    assert "坏的" in violation
    assert "合规的" not in violation


def test_host_fields_are_derived_from_the_family_model():
    """
    宿主载荷字段按「族配置模型顶层字段减去外壳字段」推导，不逐个列举。

    逐个列举会在模型加字段时漏配，而漏配的后果是那个字段静默丢失。
    """
    assert service_host_fields(ModuleType.Downloader.value) == ("path_mapping",)
    assert service_host_fields(ModuleType.MediaServer.value) == (
        "sync_interval", "sync_libraries",
    )
    assert service_host_fields(ModuleType.Notification.value) == ("switchs",)
    # 存储的裸令牌兼容指针同样是宿主消费的实例级字段，因此按同一条差集规则落进宿主载荷
    assert service_host_fields(ModuleType.Storage.value) == ("bare_token_target",)
    assert service_host_fields("不是服务族") == ()


def test_records_keep_type_payload_and_host_payload_apart():
    """整形把类型自己读的内容与宿主自己读的字段分成两列，互不混放。"""
    records = service_config_records(ModuleType.MediaServer.value, [{
        "name": "emby", "type": "emby", "enabled": True,
        "config": {"host": "e"}, "sync_libraries": ["1"], "sync_interval": 6,
    }])

    assert records[0]["config"] == {"host": "e"}
    assert records[0]["host_config"] == {"sync_libraries": ["1"], "sync_interval": 6}


def test_a_broken_row_does_not_take_down_the_rest_of_the_family(db, monkeypatch):
    """
    表里一行结构不合族配置模型时只跳过它自己，同族其余配置照常读出。

    逐条错误隔离是修过的真实缺陷，换了持久化形状之后必须仍然成立。
    """
    db.session.add_all([
        ServiceConfig(
            capability=ModuleType.MediaServer.value, type="emby", name="好的甲",
            enabled=True, config={"host": "a"},
        ),
        ServiceConfig(
            capability=ModuleType.MediaServer.value, type="emby", name="坏的",
            enabled=True, config={"host": "b"}, host_config={"sync_libraries": "不是列表"},
        ),
        ServiceConfig(
            capability=ModuleType.MediaServer.value, type="emby", name="好的乙",
            enabled=True, config={"host": "c"},
        ),
    ])
    db.session.commit()
    service = ServiceInstanceConfigService(repository=ServiceConfigOper(db=db.session))
    previous = configure_service_instance_config_reader(service.read)
    try:
        configs = service_capability_configs(ModuleType.MediaServer.value)
    finally:
        configure_service_instance_config_reader(previous)

    assert {conf.name for conf in configs} == {"好的甲", "好的乙"}


@pytest.mark.parametrize("locale", ["en-US", "zh-TW"])
def test_arbitration_message_has_a_translation(locale: str):
    """
    裁决失败的提示是用户看得到的文案，两份语言包都要收得住。

    模板与实际文案一旦对不上，翻译会静默落空——按原文返回，用例照样绿。因此断言的是
    「真实生成的那条消息翻得动」，而不是「语言包里有这么一条」。
    """
    from app.runtime.localization import LocaleHelper

    configs = [
        NotificationConf(name="甲", type="telegram", enabled=True),
        NotificationConf(name="乙", type="telegram", enabled=True),
    ]
    with pytest.raises(LookupError) as raised:
        select_instance_configs(configs, "telegram", multi_instance=False)

    translated = LocaleHelper.translate_text(str(raised.value), locale=locale)
    assert translated != str(raised.value)
    assert "telegram" in translated
    assert "甲" in translated and "乙" in translated


def test_notification_switches_still_come_from_systemconfig(monkeypatch):
    """
    通知场景开关不是实例配置，仍走 systemconfig。

    切表只搬「一份配置扇出一个具名实例」的三族；把不属于服务实例族的配置键一起改道，
    会让这些键读不出任何值。
    """
    monkeypatch.setattr(
        service_config_module,
        "_service_config_reader",
        lambda key: [{"type": MessageType.Manual.value, "action": "admin"}],
    )

    assert ServiceConfigHelper.get_notification_switch(MessageType.Manual) == "admin"
