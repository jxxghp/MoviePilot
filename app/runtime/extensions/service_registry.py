from typing import Dict, List, Optional, Type, TypeVar, Generic, Iterator

from app.runtime.extensions.module_manager import ModuleManager
from app.runtime.extensions.service_config import (
    ServiceConfigHelper,
    resolve_service_config_key,
    service_capability,
    service_instance_enabled,
)
from app.schemas.system import ServiceInfo
from app.schemas.types import SystemConfigKey

TConf = TypeVar("TConf")

__all__ = [
    "ServiceBaseHelper",
    "ServiceConfigHelper",
]


class ServiceBaseHelper(Generic[TConf]):
    """
    通用服务帮助类，抽象获取配置和服务实例的通用逻辑
    """

    def __init__(self, config_key: SystemConfigKey, conf_type: Type[TConf]):
        """绑定服务配置键与配置模型。

        配置键入参在此归一为 `SystemConfigKey` 成员：本类已随 SDK 交给扩展使用，
        取值字符串与枚举成员都是合理的写法，而取不到对应成员的键读不出任何配置，
        与其在取服务时静默返回空，不如构造时就拒绝。

        :param config_key: 服务配置键，接受 `SystemConfigKey` 成员或其取值字符串
        :param conf_type: 服务配置模型
        :raises ValueError: 配置键不是任何 `SystemConfigKey` 成员
        """
        self._modulemanager: Optional[ModuleManager] = None
        self.config_key = resolve_service_config_key(config_key)
        self.capability = service_capability(self.config_key.value)
        self.conf_type = conf_type

    @property
    def modulemanager(self) -> ModuleManager:
        """
        本族实例持有者所在的宿主模块目录

        目录首次取用时才装配：装配会按当前配置把全部宿主模块启动一遍，而模块启动
        本身要按服务令牌读配置、从而再次取用本类，构造期即装配会让两者相互嵌套。

        :return: 宿主模块目录
        """
        if self._modulemanager is None:
            self._modulemanager = ModuleManager()
        return self._modulemanager

    @modulemanager.setter
    def modulemanager(self, value: ModuleManager) -> None:
        """
        替换本族实例持有者所在的宿主模块目录

        :param value: 宿主模块目录
        :return: 无返回值
        """
        self._modulemanager = value

    def get_configs(self, include_disabled: bool = False) -> Dict[str, TConf]:
        """
        获取配置列表

        启用态按族判定：族配置模型没有启用开关字段时该族「配了即生效」，存储族即
        属此列，其开关是「有没有这条配置」本身。

        :param include_disabled: 是否包含禁用的配置，默认 False（仅返回启用的配置）
        :return: 配置字典
        """
        configs: List[TConf] = ServiceConfigHelper.get_configs(self.config_key, self.conf_type)
        return {
            config.name: config
            for config in configs
            if (config.name and config.type
                and service_instance_enabled(self.capability, config)) or include_disabled
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
        迭代消费同一服务配置的模块所持有的实例及其对应的配置，返回 ServiceInfo 实例
        """
        configs = self.get_configs()
        for module in self.modulemanager.get_service_config_modules(self.config_key.value):
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

        与 `get_services` 共用同一条筛选与优先级规则：同一实例名被多个持有者产出
        时以最后一个为准。两者若各自裁决，扩展声明的类型覆盖内建类型的场景下会
        给出互相矛盾的答案。

        :param name: 服务名称
        :param type_filter: 需要过滤的服务类型
        :return: 对应的服务信息，若不存在或类型不匹配则返回 None
        """
        if not name:
            return None
        return self.get_services(type_filter=type_filter, name_filters=[name]).get(name)
