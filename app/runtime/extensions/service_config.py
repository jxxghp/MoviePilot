"""宿主服务配置的读取端口，以及「一份服务类型按配置扇出多个具名实例」的唯一实现。

下载器、媒体服务器与消息通知按「一份配置扇出一个具名实例」消费，声明面只出现
能力标签（``downloader``/``mediaserver``/``notification``）；这三族配置存在哪张表、
按哪个模型校验，是宿主内部实现，只在本模块落地。

这三族的实例配置由服务实例配置表承载，按能力标签整族读取；其余服务相关配置键
（通知场景开关等）仍在 systemconfig 上，两条来源各有一个读取端口，由配置键属不属于
服务实例族决定走哪一条。

扇出本身也收在本模块：内建模块在 `init_module()` 时按自身类型扇出，扩展声明的类型
由服务实例注册表在取用时扇出，两条入口共用 `select_instance_configs` 与
`create_service_instance`，因此不会在「哪些配置该产出实例」和「实例按什么形状构造」
上出现分歧。两条入口的差别只在时机与缓存，不在规则。
"""

from collections.abc import Callable, Iterable
from types import MappingProxyType
from typing import Any, Dict, List, Mapping, Optional, Tuple, Type

from pydantic import ValidationError

from app.runtime.extensions.config_schema import config_value_violations
from app.runtime.extensions.instance import describe_instance_candidates
from app.runtime.log import logger
from app.schemas.system import DownloaderConf
from app.schemas.system import MediaServerConf
from app.schemas.system import NotificationConf
from app.schemas.system import NotificationSwitchConf
from app.schemas.types import MessageType, ModuleType, SystemConfigKey


ServiceConfigReader = Callable[[SystemConfigKey], Any]
ServiceInstanceConfigReader = Callable[[str], Any]

# 服务能力标签到「配置存放位置，配置模型」的映射
_SERVICE_CONFIGS: Mapping[str, Tuple[SystemConfigKey, Type]] = MappingProxyType({
    ModuleType.Downloader.value: (SystemConfigKey.Downloaders, DownloaderConf),
    ModuleType.MediaServer.value: (SystemConfigKey.MediaServers, MediaServerConf),
    ModuleType.Notification.value: (SystemConfigKey.Notifications, NotificationConf),
})

# 配置存放位置到服务能力标签的反查表，供按存放位置取用的宿主内部路径使用
_SERVICE_CAPABILITIES: Mapping[str, str] = MappingProxyType({
    config_key.value: capability
    for capability, (config_key, _conf_type) in _SERVICE_CONFIGS.items()
})

# 由宿主持有的外壳字段：实例身份、启用态、类型专属配置载荷与默认调用目标标记。
# 族配置模型上除这些之外的顶层字段都是宿主自己消费的实例级字段（路径映射、场景开关、
# 同步媒体库与同步间隔），按字段名逐个列举会在模型加字段时漏配，故按差集推导。
_INSTANCE_SHELL_FIELDS: frozenset = frozenset({"name", "type", "enabled", "config", "default"})


def _empty_service_config(_config_key: SystemConfigKey) -> Any:
    """组合根尚未装配时返回空服务配置。"""
    return None


def _empty_instance_config(_capability: str) -> Any:
    """组合根尚未装配时返回空服务实例配置。"""
    return None


_service_config_reader: ServiceConfigReader = _empty_service_config
_service_instance_config_reader: ServiceInstanceConfigReader = _empty_instance_config


def configure_service_config_reader(reader: ServiceConfigReader) -> ServiceConfigReader:
    """注入服务配置读取能力，并返回先前 reader 供隔离环境恢复。"""
    global _service_config_reader
    previous = _service_config_reader
    _service_config_reader = reader
    return previous


def configure_service_instance_config_reader(
    reader: ServiceInstanceConfigReader,
) -> ServiceInstanceConfigReader:
    """注入服务实例配置的整族读取能力，并返回先前 reader 供隔离环境恢复。

    :param reader: 按能力标签取该族全部实例配置的可调用对象
    :return: 先前登记的 reader
    """
    global _service_instance_config_reader
    previous = _service_instance_config_reader
    _service_instance_config_reader = reader
    return previous


def service_host_fields(capability: Optional[str]) -> Tuple[str, ...]:
    """返回该族由宿主消费的实例级字段名。

    :param capability: 服务能力标签
    :return: 字段名元组，按字段名升序；标签不属于任何服务族时为空元组
    """
    entry = _SERVICE_CONFIGS.get(capability) if capability else None
    if entry is None:
        return ()
    return tuple(sorted(set(entry[1].model_fields) - _INSTANCE_SHELL_FIELDS))


def resolve_service_config_key(config_key: Any) -> SystemConfigKey:
    """把服务配置键入参归一为 `SystemConfigKey` 成员。

    :param config_key: `SystemConfigKey` 成员，或与某个成员取值相同的字符串
    :return: 对应的 `SystemConfigKey` 成员
    :raises ValueError: 入参既不是成员也不等于任何成员的取值
    """
    try:
        return SystemConfigKey(config_key)
    except ValueError:
        raise ValueError(f"未知的服务配置键：{config_key!r}") from None


def service_config_key(capability: Optional[str]) -> Optional[SystemConfigKey]:
    """返回服务能力标签对应的配置键。

    实例配置本身已存进服务实例配置表，这个键留作该族配置的稳定标识：模块清单按它声明
    要监听哪一族配置，配置变更事件按它标识变的是哪一族。

    :param capability: 服务能力标签
    :return: 该族的配置键；标签不属于任何服务族时为 None
    """
    entry = _SERVICE_CONFIGS.get(capability) if capability else None
    return entry[0] if entry else None


def service_capability(config_key: Optional[str]) -> Optional[str]:
    """返回配置键对应的服务能力标签。

    :param config_key: 服务配置键
    :return: 服务能力标签；该配置键不对应任何服务实例族时为 None
    """
    return _SERVICE_CAPABILITIES.get(config_key) if config_key else None


def service_capability_configs(capability: Optional[str]) -> List:
    """按服务能力标签读取该族已通过结构校验的配置。

    :param capability: 服务能力标签
    :return: 配置列表；标签不属于任何服务族时为空列表
    """
    entry = _SERVICE_CONFIGS.get(capability) if capability else None
    if entry is None:
        return []
    config_key, conf_type = entry
    return ServiceConfigHelper.get_configs(config_key, conf_type)


def select_instance_configs(
    configs: Iterable[Any],
    service_type: Optional[str],
    *,
    multi_instance: bool = True,
) -> Dict[str, Any]:
    """按类型标识与启用态筛出一个服务类型应当扇出实例的配置。

    只认已启用、具名且类型一致的配置：取不到实例名的配置产出的实例既无法被显式
    指定，也无法被默认配置裁决选中，留着只会让实例数与可用实例数对不上。

    单实例类型被配了多份时按默认调用目标裁决：有显式默认则用它，没有默认或默认已
    停用则报错并列出候选，绝不取第一个。配置存进服务实例配置表后没有顺序列，「列表
    里排在最前的一份」不再是用户看得见也改得动的规则，因此不能再按顺序挑；报错而不是
    静默挑一个，是为了让用户看到的实例与他自己指定的目标始终一致。

    :param configs: 该族全部已通过结构校验的配置
    :param service_type: 类型标识，为空时不产出任何实例配置
    :param multi_instance: 该类型能否接受多份配置，为 False 时至多产出一个实例
    :return: 实例名到配置的映射
    :raises LookupError: 单实例类型有多份已启用配置，且裁决不出唯一的默认调用目标
    """
    if not service_type:
        return {}
    enabled = {
        conf.name: conf
        for conf in configs
        if getattr(conf, "name", None) and conf.type == service_type and conf.enabled
    }
    if multi_instance or len(enabled) <= 1:
        return enabled
    targets = [name for name, conf in enabled.items() if getattr(conf, "default", False)]
    if len(targets) == 1:
        return {targets[0]: enabled[targets[0]]}
    candidates = describe_instance_candidates(
        (conf.name, bool(getattr(conf, "enabled", True)))
        for conf in configs
        if getattr(conf, "name", None) and conf.type == service_type
    )
    raise LookupError(
        f"服务类型 {service_type} 只接受一份配置，当前已启用 {len(enabled)} 份，"
        f"且没有唯一的默认调用目标；请在设置页把其中一份设为默认调用目标，"
        f"或停用多余的配置。可选配置：{candidates}"
    )


def create_service_instance(
    name: str,
    conf: Any,
    *,
    impl: Optional[Any] = None,
    factory: Optional[Any] = None,
    config_schema: Optional[Any] = None,
) -> Any:
    """按单条用户配置构造一个具名服务实例。

    构造形状是宿主与服务类型之间的契约：``impl`` 路径由宿主填入实例名并按关键字
    展开配置内容，``factory`` 路径把整条配置对象原样交给声明方。扩展声明的契约
    校验内省的正是这两种形状，两侧共用本函数，校验过的形状即实际构造的形状。

    配置内容在构造前按类型声明的配置契约判定一次。配置写入路径已经拦过一道，这里
    仍然判定，是因为配置也可能从别的入口进来——旧版本存下的数据、直接改库、宿主
    自己的迁移，都不经过写入端点。未声明契约的类型不做判定，行为与声明该字段之前
    完全一致。

    构造失败原样抛出，由调用方决定是跳过这一条还是整体失败。

    :param name: 实例名
    :param conf: 该实例的用户配置
    :param impl: 实例实现类，与 factory 二选一
    :param factory: 实例工厂，与 impl 二选一
    :param config_schema: 该类型声明的配置契约，为 None 表示未声明
    :return: 构造出的服务实例
    :raises ValueError: 配置内容不合该类型声明的契约，或 impl 与 factory 均未给出
    """
    violations = config_value_violations(config_schema, getattr(conf, "config", None))
    if violations:
        raise ValueError(f"服务实例 {name} 的配置不合该类型的契约：{'；'.join(violations)}")
    if factory is not None:
        return factory(conf)
    if impl is None:
        raise ValueError(f"服务实例 {name} 没有可用的构造路径")
    return impl(name=name, **(getattr(conf, "config", None) or {}))


class ServiceConfigHelper:
    """读取并校验通知、下载器和媒体服务器的宿主配置。"""

    @staticmethod
    def get_configs(config_key: SystemConfigKey, conf_type: Type) -> List:
        """按指定 Schema 过滤单条非法配置，避免影响同组其它服务。

        三族实例配置按能力标签从服务实例配置表整族取，其余配置键仍从 systemconfig 取；
        两条来源之后共用同一段逐条校验，一条坏配置只跳过它自己，不影响同族其它服务。

        :param config_key: 服务配置键，接受 `SystemConfigKey` 成员或其取值字符串
        :param conf_type: 配置模型
        :return: 通过结构校验的配置列表
        :raises ValueError: 配置键不是任何 `SystemConfigKey` 成员
        """
        config_key = resolve_service_config_key(config_key)
        capability = service_capability(config_key.value)
        if capability:
            config_data = _service_instance_config_reader(capability)
        else:
            config_data = _service_config_reader(config_key)
        if not config_data:
            return []
        configs = []
        for conf in config_data:
            if not isinstance(conf, dict):
                logger.warning(f"{config_key.value} 配置格式不正确，已跳过：{conf}")
                continue
            try:
                configs.append(conf_type(**conf))
            except ValidationError as err:
                logger.error(
                    f"{config_key.value} 配置 {conf.get('name')} 校验失败，已跳过：{err}"
                )
        return configs

    @staticmethod
    def get_downloader_configs() -> List[DownloaderConf]:
        """返回已通过结构校验的下载器配置。"""
        return service_capability_configs(ModuleType.Downloader.value)

    @staticmethod
    def get_mediaserver_configs() -> List[MediaServerConf]:
        """返回已通过结构校验的媒体服务器配置。"""
        return service_capability_configs(ModuleType.MediaServer.value)

    @staticmethod
    def get_notification_configs() -> List[NotificationConf]:
        """返回已通过结构校验的通知配置。"""
        return service_capability_configs(ModuleType.Notification.value)

    @staticmethod
    def get_notification_switches() -> List[NotificationSwitchConf]:
        """返回已通过结构校验的通知场景开关。"""
        return ServiceConfigHelper.get_configs(
            SystemConfigKey.NotificationSwitchs,
            NotificationSwitchConf,
        )

    @staticmethod
    def get_notification_switch(mtype: MessageType) -> Optional[str]:
        """返回指定通知场景的目标范围。"""
        for switch in ServiceConfigHelper.get_notification_switches():
            if switch.type == mtype.value:
                return switch.action
        return None
