from collections.abc import Callable
from typing import Any, List, Optional, Type

from pydantic import ValidationError

from app.runtime.log import logger
from app.schemas.system import DownloaderConf
from app.schemas.system import MediaServerConf
from app.schemas.system import NotificationConf
from app.schemas.system import NotificationSwitchConf
from app.schemas.types import MessageType, SystemConfigKey


ServiceConfigReader = Callable[[SystemConfigKey], Any]


def _empty_service_config(_config_key: SystemConfigKey) -> Any:
    """组合根尚未装配时返回空服务配置。"""
    return None


_service_config_reader: ServiceConfigReader = _empty_service_config


def configure_service_config_reader(reader: ServiceConfigReader) -> None:
    """由启动组合根注入服务配置读取能力。"""
    global _service_config_reader
    _service_config_reader = reader


class ServiceConfigHelper:
    """读取并校验通知、下载器和媒体服务器的宿主配置。"""

    @staticmethod
    def get_configs(config_key: SystemConfigKey, conf_type: Type) -> List:
        """按指定 Schema 过滤单条非法配置，避免影响同组其它服务。"""
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
        return ServiceConfigHelper.get_configs(
            SystemConfigKey.Downloaders,
            DownloaderConf,
        )

    @staticmethod
    def get_mediaserver_configs() -> List[MediaServerConf]:
        """返回已通过结构校验的媒体服务器配置。"""
        return ServiceConfigHelper.get_configs(
            SystemConfigKey.MediaServers,
            MediaServerConf,
        )

    @staticmethod
    def get_notification_configs() -> List[NotificationConf]:
        """返回已通过结构校验的通知配置。"""
        return ServiceConfigHelper.get_configs(
            SystemConfigKey.Notifications,
            NotificationConf,
        )

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
