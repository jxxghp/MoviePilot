from typing import Dict, List, Optional, Type, TypeVar, Generic, Iterator

from app.core.module import ModuleManager
from app.db.systemconfig_oper import SystemConfigOper
from app.schemas import DownloaderConf, MediaServerConf, NotificationConf, NotificationSwitchConf, ServiceInfo
from app.schemas.types import NotificationType, SystemConfigKey, ModuleType

TConf = TypeVar("TConf")


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


class ServiceBaseHelper(Generic[TConf]):
    """
    通用服务帮助类，抽象获取配置和服务实例的通用逻辑
    """

    def __init__(self, config_key: SystemConfigKey, conf_type: Type[TConf], module_type: ModuleType):
        self.modulemanager = ModuleManager()
        self.config_key = config_key
        self.conf_type = conf_type
        self.module_type = module_type

    def get_configs(self, include_disabled: bool = False) -> Dict[str, TConf]:
        """
        获取配置列表

        :param include_disabled: 是否包含禁用的配置，默认 False（仅返回启用的配置）
        :return: 配置字典
        """
        configs: List[TConf] = ServiceConfigHelper.get_configs(self.config_key, self.conf_type)
        return {
            config.name: config
            for config in configs
            if (config.name and config.type and config.enabled) or include_disabled
        } if configs else {}

    def get_config(self, name: str) -> Optional[TConf]:
        """
        获取指定名称配置
        """
        if not name:
            return None
        configs = self.get_configs()
        return configs.get(name)

    def iterate_module_instances(self) -> Iterator[ServiceInfo]:
        """
        迭代所有模块的实例及其对应的配置，返回 ServiceInfo 实例
        """
        configs = self.get_configs()
        modules = self.modulemanager.get_running_type_modules(self.module_type)
        for module in modules:
            if not module:
                continue
            module_instances = module.get_instances()
            if not isinstance(module_instances, dict):
                continue
            for name, instance in module_instances.items():
                if not instance:
                    continue
                config = configs.get(name)
                service_info = ServiceInfo(
                    name=name,
                    instance=instance,
                    module=module,
                    type=config.type if config else None,
                    config=config
                )
                yield service_info

    def get_services(self, type_filter: Optional[str] = None, name_filters: Optional[List[str]] = None) \
            -> Dict[str, ServiceInfo]:
        """
        获取服务信息列表，并根据类型和名称列表进行过滤

        :param type_filter: 需要过滤的服务类型
        :param name_filters: 需要过滤的服务名称列表
        :return: 过滤后的服务信息字典
        """
        name_filters_set = set(name_filters) if name_filters else None

        return {
            service_info.name: service_info
            for service_info in self.iterate_module_instances()
            if service_info.config and (
                    type_filter is None or service_info.type == type_filter
            ) and (
                       name_filters_set is None or service_info.name in name_filters_set)
        }

    def get_service(self, name: str, type_filter: Optional[str] = None) -> Optional[ServiceInfo]:
        """
        获取指定名称的服务信息，并根据类型过滤

        :param name: 服务名称
        :param type_filter: 需要过滤的服务类型
        :return: 对应的服务信息，若不存在或类型不匹配则返回 None
        """
        if not name:
            return None
        for service_info in self.iterate_module_instances():
            if service_info.name == name:
                if service_info.config and (type_filter is None or service_info.type == type_filter):
                    return service_info
        return None
