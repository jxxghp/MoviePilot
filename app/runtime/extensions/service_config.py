"""宿主服务配置的读取端口，以及服务能力标签与配置存放位置的唯一对照。

下载器、媒体服务器与消息通知按「一份配置扇出一个具名实例」消费，声明面只出现
能力标签（``downloader``/``mediaserver``/``notification``）；这三族配置分别存放在
systemconfig 的哪个键、按哪个模型校验，是宿主内部实现，只在本模块落地。
"""

from collections.abc import Callable
from types import MappingProxyType
from typing import Any, List, Mapping, Optional, Tuple, Type

from pydantic import ValidationError

from app.runtime.log import logger
from app.schemas.system import DownloaderConf
from app.schemas.system import MediaServerConf
from app.schemas.system import NotificationConf
from app.schemas.system import NotificationSwitchConf
from app.schemas.types import MessageType, ModuleType, SystemConfigKey


ServiceConfigReader = Callable[[SystemConfigKey], Any]

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


def _empty_service_config(_config_key: SystemConfigKey) -> Any:
    """组合根尚未装配时返回空服务配置。"""
    return None


_service_config_reader: ServiceConfigReader = _empty_service_config


def configure_service_config_reader(reader: ServiceConfigReader) -> ServiceConfigReader:
    """注入服务配置读取能力，并返回先前 reader 供隔离环境恢复。"""
    global _service_config_reader
    previous = _service_config_reader
    _service_config_reader = reader
    return previous


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
    """返回服务能力标签对应的配置存放位置。

    :param capability: 服务能力标签
    :return: 该族配置在 systemconfig 中的键；标签不属于任何服务族时为 None
    """
    entry = _SERVICE_CONFIGS.get(capability) if capability else None
    return entry[0] if entry else None


def service_capability(config_key: Optional[str]) -> Optional[str]:
    """返回配置存放位置对应的服务能力标签。

    :param config_key: 服务配置在 systemconfig 中的存放位置
    :return: 服务能力标签；该存放位置不对应任何服务族时为 None
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


class ServiceConfigHelper:
    """读取并校验通知、下载器和媒体服务器的宿主配置。"""

    @staticmethod
    def get_configs(config_key: SystemConfigKey, conf_type: Type) -> List:
        """按指定 Schema 过滤单条非法配置，避免影响同组其它服务。

        :param config_key: 服务配置键，接受 `SystemConfigKey` 成员或其取值字符串
        :param conf_type: 配置模型
        :return: 通过结构校验的配置列表
        :raises ValueError: 配置键不是任何 `SystemConfigKey` 成员
        """
        config_key = resolve_service_config_key(config_key)
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
