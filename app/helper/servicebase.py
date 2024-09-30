from typing import Dict, List, Optional, Type, TypeVar, Generic, Iterator

from app.core.module import ModuleManager
from app.helper.serviceconfig import ServiceConfigHelper
from app.schemas import ServiceInfo
from app.schemas.types import SystemConfigKey

TConf = TypeVar("TConf")


class ServiceBaseHelper(Generic[TConf]):
    """
    通用服务帮助类，抽象获取配置和服务实例的通用逻辑
    """

    def __init__(self, config_key: SystemConfigKey, conf_type: Type[TConf], modules: List[str]):
        self.modulemanager = ModuleManager()
        self.config_key = config_key
        self.conf_type = conf_type
        self.modules = modules

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
        for module_name in self.modules:
            module = self.modulemanager.get_running_module(module_name)
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

    def get_services(self, type_filter: Optional[str] = None) -> Dict[str, ServiceInfo]:
        """
        获取服务信息列表，并根据类型过滤

        :param type_filter: 需要过滤的服务类型
        :return: 过滤后的服务信息字典
        """
        return {
            service_info.name: service_info
            for service_info in self.iterate_module_instances()
            if service_info.config and (type_filter is None or service_info.type == type_filter)
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
