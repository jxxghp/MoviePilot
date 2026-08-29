"""下载器、媒体服务器和通知服务的应用层目录端口。"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any, Dict, Generic, List, Optional, Type, TypeVar

from app.schemas.system import ServiceInfo
from app.schemas.types import ModuleType, SystemConfigKey

TConf = TypeVar("TConf")
ServiceConfigLoader = Callable[[SystemConfigKey, Type[Any]], list[Any]]
RunningModuleLoader = Callable[[ModuleType], list[Any]]


def _unconfigured_configs(
    _config_key: SystemConfigKey,
    _conf_type: Type[Any],
) -> list[Any]:
    """拒绝在启动组合根装配前隐式读取服务配置。"""
    raise RuntimeError("服务配置目录尚未由启动组合根配置")


def _unconfigured_modules(_module_type: ModuleType) -> list[Any]:
    """拒绝在启动组合根装配前隐式抓取模块管理器。"""
    raise RuntimeError("运行模块目录尚未由启动组合根配置")


_config_loader: ServiceConfigLoader = _unconfigured_configs
_module_loader: RunningModuleLoader = _unconfigured_modules


def configure_service_directory(
    *,
    configs: ServiceConfigLoader,
    modules: RunningModuleLoader,
) -> None:
    """由启动组合根注入服务配置和运行模块枚举端口。"""
    global _config_loader, _module_loader
    _config_loader = configs
    _module_loader = modules


def reset_service_directory() -> None:
    """恢复未装配服务目录，禁止跨 lifespan 复用旧模块对象。"""
    global _config_loader, _module_loader
    _config_loader = _unconfigured_configs
    _module_loader = _unconfigured_modules


def get_service_configs(
    config_key: SystemConfigKey,
    conf_type: Type[TConf],
) -> list[TConf]:
    """通过组合根登记的读取器返回已校验服务配置。"""
    return _config_loader(config_key, conf_type)


class ServiceBaseHelper(Generic[TConf]):
    """通过应用端口查询服务配置和对应运行实例。"""

    def __init__(
        self,
        config_key: SystemConfigKey,
        conf_type: Type[TConf],
        module_type: ModuleType,
    ) -> None:
        """绑定配置类型和模块能力类型，不抓取具体 Runtime 管理器。"""
        self.config_key = config_key
        self.conf_type = conf_type
        self.module_type = module_type

    def get_configs(self, include_disabled: bool = False) -> Dict[str, TConf]:
        """返回按名称索引的有效服务配置。"""
        configs = get_service_configs(self.config_key, self.conf_type)
        return {
            config.name: config
            for config in configs
            if config.name
            and config.type
            and (config.enabled or include_disabled)
        }

    def get_config(self, name: str) -> Optional[TConf]:
        """按名称返回单个启用服务配置。"""
        return self.get_configs().get(name) if name else None

    def iterate_module_instances(self) -> Iterator[ServiceInfo]:
        """迭代当前类型所有运行模块实例及其配置投影。"""
        configs = self.get_configs()
        for module in _module_loader(self.module_type):
            if not module:
                continue
            instances = module.get_instances()
            if not isinstance(instances, dict):
                continue
            for name, instance in instances.items():
                if not instance:
                    continue
                config = configs.get(name)
                yield ServiceInfo(
                    name=name,
                    instance=instance,
                    module=module,
                    type=config.type if config else None,
                    config=config,
                )

    def get_services(
        self,
        type_filter: Optional[str] = None,
        name_filters: Optional[List[str]] = None,
    ) -> Dict[str, ServiceInfo]:
        """按服务类型和名称集合过滤运行实例。"""
        names = set(name_filters) if name_filters else None
        return {
            service.name: service
            for service in self.iterate_module_instances()
            if service.config
            and (type_filter is None or service.type == type_filter)
            and (names is None or service.name in names)
        }

    def get_service(
        self,
        name: str,
        type_filter: Optional[str] = None,
    ) -> Optional[ServiceInfo]:
        """按名称和可选类型返回单个运行服务。"""
        if not name:
            return None
        for service in self.iterate_module_instances():
            if (
                service.name == name
                and service.config
                and (type_filter is None or service.type == type_filter)
            ):
                return service
        return None
