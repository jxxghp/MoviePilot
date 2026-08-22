"""服务实例扇出：内建模块与扩展声明的类型共用同一份筛选与构造。

判据见 docs/plugin-extension-architecture.md 第 7.1 节。宿主内「一份服务类型按 N 条
用户配置扇出 N 个具名实例」只有一份实现，内建模块与扩展声明的类型只是入口不同：
两侧按同一规则筛配置（同类型、已启用、具名），按同一形状构造实例（``impl`` 关键字
展开配置内容、``factory`` 接整条配置），单条配置构造失败都只跳过它自己。
"""

from typing import Any, Dict, Iterator, List, Optional

import pytest

from app.modules import _MessageBase
from app.runtime.extensions import service_config as service_config_module
from app.runtime.extensions.registry import service_instance as registry_module
from app.runtime.extensions.contract.extension import ExtensionDistribution
from app.runtime.extensions.service_config import (
    create_service_instance,
    select_instance_configs,
)
from app.runtime.extensions.registry.service_instance import (
    ServiceInstanceAdapter,
    ServiceInstanceEntry,
)
from app.schemas.system import NotificationConf
from app.schemas.types import ModuleType, SystemConfigKey


class _FragileClient:
    """按关键字构造的服务客户端桩，host 为 bad 时构造失败。"""

    def __init__(self, name: Optional[str] = None, host: Optional[str] = None, **kwargs: Any):
        """记录实例名与配置内容，坏配置直接抛错。

        :param name: 宿主填入的实例名
        :param host: 配置内容中的地址
        :param kwargs: 其余配置内容
        """
        if host == "bad":
            raise ValueError("无法连接")
        self.name = name
        self.host = host
        self.extra = kwargs


class _SampleMessage(_MessageBase[object]):
    """通知族服务基类的最小实现，用于驱动内建侧扇出。"""


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


@pytest.fixture
def service_configs(monkeypatch) -> Iterator[Dict[str, list]]:
    """接管服务配置读取端口，用例改写字典即改写用户配置。"""
    values: Dict[str, list] = {}

    def reader(capability: str) -> Any:
        """按能力标签返回用例写入的原始配置列表。"""
        config_key = service_config_module.service_config_key(capability)
        return values.get(config_key.value) if config_key else None

    monkeypatch.setattr(
        service_config_module, "_service_instance_config_reader", reader
    )
    yield values


@pytest.fixture
def builtin_logger(monkeypatch) -> _RecordingLogger:
    """接管内建服务基类的日志端口。"""
    log = _RecordingLogger()
    monkeypatch.setattr("app.modules.logger", log)
    return log


@pytest.fixture
def extension_logger(monkeypatch) -> _RecordingLogger:
    """接管服务实例注册表的日志端口。"""
    log = _RecordingLogger()
    monkeypatch.setattr(registry_module, "logger", log)
    return log


def _notification_config(name: Optional[str], service_type: str, enabled: bool = True,
                         **config: Any) -> dict:
    """构造一条通知配置的原始字典。

    :param name: 实例名
    :param service_type: 类型标识
    :param enabled: 是否启用
    :param config: 该实例的配置内容
    :return: 与持久化形状一致的配置字典
    """
    return {"name": name, "type": service_type, "enabled": enabled, "config": config}


def _extension_adapter(**overrides: Any) -> ServiceInstanceAdapter:
    """构造一个通知族的扩展服务实例适配器。

    :param overrides: 覆盖登记项的字段
    :return: 适配器
    """
    fields: Dict[str, Any] = {
        "capability": ModuleType.Notification.value,
        "service_type": "wechat",
        "name": "微信",
        "distribution": ExtensionDistribution.MARKET,
        "owner": "demo",
        "impl": _FragileClient,
    }
    fields.update(overrides)
    return ServiceInstanceAdapter(ServiceInstanceEntry(**fields))


def test_builtin_fanout_skips_only_the_failing_config(
    service_configs: Dict[str, list], builtin_logger: _RecordingLogger
) -> None:
    """内建侧一条配置构造失败只跳过它自己，同类型其余实例照常产出。"""
    service_configs[SystemConfigKey.Notifications.value] = [
        _notification_config("可用", "wechat", host="ok"),
        _notification_config("坏配置", "wechat", host="bad"),
        _notification_config("另一个", "wechat", host="ok2"),
    ]

    service = _SampleMessage()
    service.init_service("wechat", service_type=_FragileClient)

    assert set(service.get_instances()) == {"可用", "另一个"}
    assert any("坏配置" in message for message in builtin_logger.errors)


def test_extension_fanout_skips_only_the_failing_config(
    service_configs: Dict[str, list], extension_logger: _RecordingLogger
) -> None:
    """扩展侧的错误隔离与内建侧同规则。"""
    service_configs[SystemConfigKey.Notifications.value] = [
        _notification_config("可用", "wechat", host="ok"),
        _notification_config("坏配置", "wechat", host="bad"),
    ]

    adapter = _extension_adapter()

    assert set(adapter.get_instances()) == {"可用"}
    assert any("坏配置" in message for message in extension_logger.errors)


def test_both_sides_ignore_unnamed_configs(service_configs: Dict[str, list]) -> None:
    """取不到实例名的配置两侧都不产出实例，否则实例既无法被指定也无法被裁决选中。"""
    service_configs[SystemConfigKey.Notifications.value] = [
        _notification_config(None, "wechat", host="ok"),
        _notification_config("具名", "wechat", host="ok"),
    ]

    service = _SampleMessage()
    service.init_service("wechat", service_type=_FragileClient)
    adapter = _extension_adapter()

    assert set(service.get_instances()) == {"具名"}
    assert set(adapter.get_instances()) == {"具名"}


def test_both_sides_ignore_other_types_and_disabled_configs(
    service_configs: Dict[str, list]
) -> None:
    """两侧都只认同类型且已启用的配置。"""
    service_configs[SystemConfigKey.Notifications.value] = [
        _notification_config("别的类型", "telegram", host="ok"),
        _notification_config("已停用", "wechat", enabled=False, host="ok"),
        _notification_config("生效", "wechat", host="ok"),
    ]

    service = _SampleMessage()
    service.init_service("wechat", service_type=_FragileClient)
    adapter = _extension_adapter()

    assert set(service.get_instances()) == {"生效"}
    assert set(adapter.get_instances()) == {"生效"}


def test_both_sides_construct_impl_with_name_and_expanded_config(
    service_configs: Dict[str, list]
) -> None:
    """impl 路径两侧都按 ``impl(name=实例名, **配置内容)`` 构造。"""
    service_configs[SystemConfigKey.Notifications.value] = [
        _notification_config("目标", "wechat", host="ok", token="t"),
    ]

    service = _SampleMessage()
    service.init_service("wechat", service_type=_FragileClient)
    adapter = _extension_adapter()

    for instances in (service.get_instances(), adapter.get_instances()):
        client = instances["目标"]
        assert client.name == "目标"
        assert client.host == "ok"
        assert client.extra == {"token": "t"}


def test_both_sides_hand_the_whole_config_to_a_factory(
    service_configs: Dict[str, list]
) -> None:
    """factory 路径两侧都把整条配置对象原样交给工厂。"""
    service_configs[SystemConfigKey.Notifications.value] = [
        _notification_config("目标", "wechat", host="ok"),
    ]

    service = _SampleMessage()
    service.init_service("wechat", service_type=lambda conf: conf)
    adapter = _extension_adapter(impl=None, factory=lambda conf: conf)

    for instances in (service.get_instances(), adapter.get_instances()):
        conf = instances["目标"]
        assert isinstance(conf, NotificationConf)
        assert conf.name == "目标"
        assert conf.config == {"host": "ok"}


def test_select_instance_configs_uses_the_default_target_for_single_instance_types() -> None:
    """单实例类型配了多份时用显式的默认调用目标，与顺序无关。"""
    configs = [
        NotificationConf(name="第一", type="wechat", enabled=True),
        NotificationConf(name="第二", type="wechat", enabled=True, default=True),
    ]

    selected = select_instance_configs(configs, "wechat", multi_instance=False)

    assert list(selected) == ["第二"]


def test_select_instance_configs_refuses_to_guess_without_a_default_target() -> None:
    """没有默认调用目标时报错并列出候选，绝不取第一个。"""
    configs = [
        NotificationConf(name="第一", type="wechat", enabled=True),
        NotificationConf(name="第二", type="wechat", enabled=True),
    ]

    with pytest.raises(LookupError) as raised:
        select_instance_configs(configs, "wechat", multi_instance=False)

    message = str(raised.value)
    assert "第一" in message and "第二" in message


def test_select_instance_configs_refuses_when_the_default_target_is_disabled() -> None:
    """默认调用目标已停用等同于没有默认，报错而不是改走另一份配置。"""
    configs = [
        NotificationConf(name="停用的默认", type="wechat", enabled=False, default=True),
        NotificationConf(name="甲", type="wechat", enabled=True),
        NotificationConf(name="乙", type="wechat", enabled=True),
    ]

    with pytest.raises(LookupError) as raised:
        select_instance_configs(configs, "wechat", multi_instance=False)

    message = str(raised.value)
    assert "停用的默认" in message and "甲" in message and "乙" in message


def test_select_instance_configs_ignores_the_default_target_for_multi_instance_types() -> None:
    """多实例类型按配置逐条扇出，默认标记不裁掉任何一份。"""
    configs = [
        NotificationConf(name="第一", type="wechat", enabled=True),
        NotificationConf(name="第二", type="wechat", enabled=True, default=True),
    ]

    selected = select_instance_configs(configs, "wechat")

    assert sorted(selected) == ["第一", "第二"]


def test_select_instance_configs_needs_a_type() -> None:
    """类型标识为空时不产出任何实例配置。"""
    configs = [NotificationConf(name="第一", type="wechat", enabled=True)]

    assert select_instance_configs(configs, None) == {}


def test_create_service_instance_requires_a_construction_path() -> None:
    """既无 impl 也无 factory 时构造不成立，以异常呈现而不是静默返回空。"""
    conf = NotificationConf(name="目标", type="wechat", enabled=True)

    with pytest.raises(ValueError):
        create_service_instance("目标", conf)
