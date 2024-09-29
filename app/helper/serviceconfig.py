from typing import List, Type, Optional

from app.db.systemconfig_oper import SystemConfigOper
from app.schemas import DownloaderConf, MediaServerConf, NotificationConf, NotificationSwitchConf
from app.schemas.types import SystemConfigKey, NotificationType


class ServiceConfigHelper:
    """
    配置帮助类，获取不同类型的服务配置
    """

    @staticmethod
    def get_configs(config_key: SystemConfigKey, conf_type: Type) -> List:
        """
        通用获取配置的方法，根据 config_key 获取相应的配置并返回指定类型的配置列表

        :param config_key: 系统配置的 key
        :param conf_type: 用于实例化配置对象的类类型
        :return: 配置对象列表
        """
        config_data = SystemConfigOper().get(config_key)
        if not config_data:
            return []
        # 直接使用 conf_type 来实例化配置对象
        return [conf_type(**conf) for conf in config_data]

    @staticmethod
    def get_downloader_configs() -> List[DownloaderConf]:
        """
        获取下载器的配置
        """
        return ServiceConfigHelper.get_configs(SystemConfigKey.Downloaders, DownloaderConf)

    @staticmethod
    def get_mediaserver_configs() -> List[MediaServerConf]:
        """
        获取媒体服务器的配置
        """
        return ServiceConfigHelper.get_configs(SystemConfigKey.MediaServers, MediaServerConf)

    @staticmethod
    def get_notification_configs() -> List[NotificationConf]:
        """
        获取消息通知渠道的配置
        """
        return ServiceConfigHelper.get_configs(SystemConfigKey.Notifications, NotificationConf)

    @staticmethod
    def get_notification_switches() -> List[NotificationSwitchConf]:
        """
        获取消息通知场景的开关
        """
        return ServiceConfigHelper.get_configs(SystemConfigKey.NotificationSwitchs, NotificationSwitchConf)

    @staticmethod
    def get_notification_switch(mtype: NotificationType) -> Optional[str]:
        """
        获取指定类型的消息通知场景的开关
        """
        switchs = ServiceConfigHelper.get_notification_switches()
        for switch in switchs:
            if switch.type == mtype.value:
                return switch.action
        return None
