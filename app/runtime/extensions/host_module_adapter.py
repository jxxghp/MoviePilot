from __future__ import annotations

import importlib
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Optional

from app.runtime.capabilities.model import (
    ActivationPolicy,
    AdapterExecutionMode,
    CapabilitySpec,
    SelectorSchema,
)
from app.runtime.capabilities.registry import CapabilityRegistry
from app.runtime.config import settings
from app.runtime.extensions.contract import (
    ExtensionDistribution,
    ExtensionFaultScope,
    ExtensionProvider,
    extension_capability_names,
    is_implemented_callable,
    supports_extension_hook,
)
from app.runtime.extensions.service_config import ServiceConfigHelper
from app.runtime.log import logger
from app.schemas.types import SystemConfigKey


HOST_MODULE_KIND = "host_module"
# 扩展提供的模块与宿主模块共用声明格式、适配器与分发路径，只以 kind 区分来源：
# 宿主模块受「一级模块包与清单一一对应」的库存合同约束，扩展的声明根在插件源码
# 目录下，不适用该合同。
PLUGIN_MODULE_KIND = "plugin_module"
_SETTING_SELECTOR = "setting_truthy"
_SERVICE_SELECTOR = "system_config_item"
_MODULE_ROOT = Path(__file__).resolve().parents[2] / "modules"
_SERVICE_CONFIG_GETTERS = MappingProxyType({
    SystemConfigKey.Downloaders.value: ServiceConfigHelper.get_downloader_configs,
    SystemConfigKey.MediaServers.value: ServiceConfigHelper.get_mediaserver_configs,
    SystemConfigKey.Notifications.value: ServiceConfigHelper.get_notification_configs,
})
# 模块 manifest 必须声明的元数据字段
_REQUIRED_METADATA_FIELDS = frozenset({"name", "priority"})
# 只有按服务配置扇出多实例的模块才声明的元数据字段
_SERVICE_CONFIG_FIELD = "service_config"
# subtype 是可选元数据字段，声明时必须是非空字符串；渠道标识的取值不
# 再要求登记于内核枚举，是否声明由模块自身决定
_SUBTYPE_FIELD = "subtype"
_NOTIFICATION_CONFIG_KEY = SystemConfigKey.Notifications.value


@dataclass(frozen=True, slots=True)
class HostModuleConfigSnapshot:
    """一次 reconcile 使用的不可变配置视图，避免每个能力重复查询配置。"""

    settings: Mapping[str, Any]
    services: Mapping[str, tuple[Any, ...]]


def _validate_setting_selector(config: Mapping[str, Any]) -> None:
    """限制 setting selector 只能读取已声明的应用设置。"""
    key = config["key"]
    if not isinstance(key, str) or not key or not hasattr(settings, key):
        raise ValueError(f"未知应用设置：{key!r}")


def _validate_service_selector(config: Mapping[str, Any]) -> None:
    """限制服务 selector 使用经过 Schema 校验的三个宿主服务配置。"""
    key = config["key"]
    if key not in _SERVICE_CONFIG_GETTERS:
        raise ValueError(f"不支持的服务配置：{key!r}")
    if config["match_field"] != "type":
        raise ValueError("服务 selector 的 match_field 必须是 type")
    if config["enabled_field"] != "enabled":
        raise ValueError("服务 selector 的 enabled_field 必须是 enabled")
    match_value = config["match_value"]
    if not isinstance(match_value, str) or not match_value:
        raise ValueError("服务 selector 的 match_value 必须是非空字符串")


HOST_MODULE_SELECTOR_SCHEMAS = MappingProxyType({
    _SETTING_SELECTOR: SelectorSchema(
        required_fields=frozenset({"key"}),
        validator=_validate_setting_selector,
    ),
    _SERVICE_SELECTOR: SelectorSchema(
        required_fields=frozenset({
            "key",
            "match_field",
            "match_value",
            "enabled_field",
        }),
        validator=_validate_service_selector,
    ),
})


def _validate_manifest_inventory(registry: CapabilityRegistry) -> None:
    """校验一级模块包与 manifest 一一对应，并固定宿主声明合同。

    库存合同只约束宿主模块：宿主模块的声明根固定为 ``app/modules``，包与清单必须
    一一对应；扩展提供的模块声明在各自插件源码目录下，数量与目录结构由扩展自己
    决定，因此按 kind 分流校验。
    """
    module_packages = {
        child.name
        for child in _MODULE_ROOT.iterdir()
        if child.is_dir()
        and not child.name.startswith("_")
        and (child / "__init__.py").is_file()
    }
    specs = tuple(
        spec for spec in registry.list_specs() if spec.kind == HOST_MODULE_KIND
    )
    manifest_packages = {spec.source.parent.name for spec in specs}
    if module_packages != manifest_packages:
        missing = sorted(module_packages - manifest_packages)
        unknown = sorted(manifest_packages - module_packages)
        raise ValueError(
            f"Host Module manifest inventory 不一致：missing={missing} unknown={unknown}"
        )

    for spec in specs:
        if spec.source.parent.parent != _MODULE_ROOT:
            raise ValueError(f"Host Module manifest 必须位于一级模块包：{spec.source}")
        module_name, symbol_name = spec.entrypoint.split(":", maxsplit=1)
        expected_module = f"app.modules.{spec.source.parent.name}"
        if module_name != expected_module or symbol_name != spec.id:
            raise ValueError(
                f"{spec.source}: entrypoint 必须指向同包且类名等于 capability id"
            )
        metadata_fields = set(spec.metadata)
        missing_metadata = _REQUIRED_METADATA_FIELDS - metadata_fields
        unknown_metadata = (
            metadata_fields
            - _REQUIRED_METADATA_FIELDS
            - {_SERVICE_CONFIG_FIELD, _SUBTYPE_FIELD}
        )
        if missing_metadata or unknown_metadata:
            raise ValueError(
                f"{spec.source}: metadata 字段非法，"
                f"missing={sorted(missing_metadata)} unknown={sorted(unknown_metadata)}"
            )
        service_config = spec.metadata.get(_SERVICE_CONFIG_FIELD)
        if service_config is not None:
            if service_config not in _SERVICE_CONFIG_GETTERS:
                raise ValueError(
                    f"{spec.source}: 非法 metadata.service_config={service_config!r}"
                )
            if service_config not in spec.watch:
                raise ValueError(
                    f"{spec.source}: activation.watch 必须包含 metadata.service_config"
                )
        subtype = spec.metadata.get(_SUBTYPE_FIELD)
        if subtype is not None and (not isinstance(subtype, str) or not subtype):
            raise ValueError(f"{spec.source}: metadata.subtype 必须是非空字符串")
        if service_config == _NOTIFICATION_CONFIG_KEY and subtype is None:
            raise ValueError(
                f"{spec.source}: 通知渠道模块必须声明 metadata.subtype"
            )
        priority = spec.metadata["priority"]
        if isinstance(priority, bool) or not isinstance(priority, int):
            raise ValueError(f"{spec.source}: metadata.priority 必须是整数")
        if spec.activation is ActivationPolicy.WHEN_CONFIGURED:
            selector_key = str(spec.selector.config["key"])
            if selector_key not in spec.watch:
                raise ValueError(
                    f"{spec.source}: activation.watch 必须包含 selector 配置键"
                )


def build_host_module_registry(
    extra_roots: Iterable[Path] = (),
) -> CapabilityRegistry:
    """从现有物理模块包与扩展声明根构建 import-free 模块注册表。

    扩展声明根由调用方给出，通常是各扩展当前生效版本的源码目录。声明格式、适配器
    与分发路径都与宿主模块一致，扩展据此获得与内置模块同级的能力注册面；不传扩展
    根时结果与只扫描宿主模块包完全一致。
    :param extra_roots: 追加的能力声明根，目录不存在或没有清单时由发现流程报错
    :return: 含宿主模块与扩展模块声明的注册表
    """
    registry = CapabilityRegistry.discover(
        (_MODULE_ROOT, *extra_roots),
        kinds={HOST_MODULE_KIND, PLUGIN_MODULE_KIND},
        selector_schemas=HOST_MODULE_SELECTOR_SCHEMAS,
    )
    _validate_manifest_inventory(registry)
    return registry


def capture_host_module_config(
    specs: tuple[CapabilitySpec, ...],
) -> HostModuleConfigSnapshot:
    """对本轮涉及的设置和服务配置各读取一次并冻结容器。"""
    setting_keys: set[str] = set()
    service_keys: set[str] = set()
    for spec in specs:
        selector = spec.selector
        if selector is None:
            continue
        key = str(selector.config["key"])
        if selector.kind == _SETTING_SELECTOR:
            setting_keys.add(key)
        elif selector.kind == _SERVICE_SELECTOR:
            service_keys.add(key)

    setting_values = {
        key: getattr(settings, key)
        for key in sorted(setting_keys)
    }
    service_values = {
        key: tuple(_SERVICE_CONFIG_GETTERS[key]())
        for key in sorted(service_keys)
    }
    return HostModuleConfigSnapshot(
        settings=MappingProxyType(setting_values),
        services=MappingProxyType(service_values),
    )


def should_run_host_module(
    spec: CapabilitySpec,
    snapshot: HostModuleConfigSnapshot,
) -> bool:
    """依据有限 selector 语法判断能力是否应拥有运行资源。"""
    if spec.activation is ActivationPolicy.BOOTSTRAP:
        return True
    if spec.activation is ActivationPolicy.ON_FIRST_USE:
        return False
    selector = spec.selector
    if selector is None:
        return False
    if selector.kind == _SETTING_SELECTOR:
        return bool(snapshot.settings[selector.config["key"]])
    if selector.kind == _SERVICE_SELECTOR:
        config = selector.config
        return any(
            getattr(item, config["match_field"]) == config["match_value"]
            and bool(getattr(item, config["enabled_field"]))
            for item in snapshot.services[config["key"]]
        )
    raise ValueError(f"未支持的 Host Module selector：{selector.kind}")


class HostModuleExtension:
    """把运行中的宿主模块实例投影为扩展视图。"""

    distribution = ExtensionDistribution.BUILTIN
    fault_scope = ExtensionFaultScope.HOST

    def __init__(self, instance: Any) -> None:
        """保存被投影的宿主模块实例。

        :param instance: 运行中的宿主模块实例
        """
        self.instance = instance

    @property
    def extension_id(self) -> str:
        """返回模块类名作为稳定标识。"""
        return self.instance.__class__.__name__

    @property
    def display_name(self) -> str:
        """返回模块展示名，读取失败时回退到稳定标识。"""
        try:
            return self.instance.get_name()
        except Exception as err:
            logger.debug("获取模块名称出错：%s", str(err))
            return self.extension_id

    @property
    def priority(self) -> int:
        """返回模块在同一能力下的仲裁顺序。"""
        return self.instance.get_priority()

    def is_enabled(self) -> bool:
        """宿主模块的启用与否由配置决定，能拿到运行实例即为启用。"""
        return True

    def initialize(self, config: Optional[dict] = None) -> None:
        """初始化模块自有的连接、线程或客户端资源。

        :param config: 扩展配置；宿主模块的配置来自应用设置，此入参不参与初始化
        :return: 无返回值
        """
        del config
        self.instance.init_module()

    def terminate(self) -> None:
        """停止模块自有的连接、线程或客户端资源。"""
        self.instance.stop()

    def self_test(self) -> Optional[tuple]:
        """执行模块连通性自检。

        :return: `(是否成功, 错误信息)`；模块未给出结论时为 ``None``
        """
        return self.instance.test()

    def supports_hook(self, name: str) -> bool:
        """判断模块是否实现了指定扩展点。

        :param name: 扩展点名称
        :return: 该扩展点已实现时为 True
        """
        return supports_extension_hook(self.instance, name)

    def capability_names(self) -> tuple[str, ...]:
        """列出模块可被分发触达的方法名。

        :return: 已实现的公开方法名元组
        """
        return extension_capability_names(self.instance)

    def capability(self, name: str) -> Optional[Any]:
        """取用模块的指定可分发方法。

        :param name: 方法名称
        :return: 已实现的方法；未提供时为 ``None``
        """
        candidate = getattr(self.instance, name, None)
        return candidate if is_implemented_callable(candidate) else None


class HostModuleProviderSource:
    """把运行中的宿主模块目录投影为分发提供者。"""

    distribution = ExtensionDistribution.BUILTIN

    def __init__(self, catalog: Any) -> None:
        """保存宿主模块目录端口。

        :param catalog: 提供运行中宿主模块查询的目录
        """
        self._catalog = catalog

    @staticmethod
    def announce_phase(method: str) -> None:
        """记录宿主模块参与接力的阶段日志。

        :param method: 模块方法名称
        :return: 无返回值
        """
        logger.debug("请求系统模块执行：%s ...", method)

    def notify_providers(self, method: str):
        """线性扫描全部运行模块，按优先级升序产出提供者。

        :param method: 模块方法名称
        :return: 提供者迭代器
        """
        modules = sorted(
            self._catalog.get_running_modules(method),
            key=lambda module: module.get_priority(),
        )
        for module in modules:
            yield self._provider(module, method)

    def answer_providers(self, method: str):
        """按能力索引产出已排序的提供者。

        :param method: 模块方法名称
        :return: 提供者迭代器
        """
        for module in self._catalog.providers_for(method):
            yield self._provider(module, method)

    @staticmethod
    def _provider(instance: Any, method: str) -> ExtensionProvider:
        """把一个运行模块的方法包装成提供者记录。

        :param instance: 运行中的宿主模块实例
        :param method: 模块方法名称
        :return: 提供者记录
        """
        extension = HostModuleExtension(instance)
        return ExtensionProvider(
            extension_id=extension.extension_id,
            display_name=extension.display_name,
            distribution=ExtensionDistribution.BUILTIN,
            fault_scope=ExtensionFaultScope.HOST,
            invoke=getattr(instance, method),
            relays_result=True,
        )


class HostModuleAdapter:
    """把现有模块类接入 Capability Runtime，保持类路径与对象 identity 不变。"""

    execution_mode = AdapterExecutionMode.SYNC

    @staticmethod
    def materialize(spec: CapabilitySpec) -> type:
        """按 manifest entrypoint 导入并返回原始模块类。"""
        module_name, symbol_name = spec.entrypoint.split(":", maxsplit=1)
        implementation = getattr(importlib.import_module(module_name), symbol_name)
        if not isinstance(implementation, type):
            raise TypeError(f"{spec.entrypoint} 不是模块类")
        return implementation

    @staticmethod
    def create(
        spec: CapabilitySpec,
        implementation: type,
        generation: int,
        previous: Any = None,
    ) -> Any:
        """首次创建实例；配置重载继续使用原实例以保留既有模块语义。"""
        del spec, generation
        return previous if previous is not None else implementation()

    @staticmethod
    def start(spec: CapabilitySpec, candidate: Any, generation: int) -> None:
        """初始化候选实例拥有的连接、线程或客户端资源。"""
        del spec, generation
        candidate.init_module()

    @staticmethod
    def stop(spec: CapabilitySpec, instance: Any, generation: int) -> None:
        """停止实例拥有的资源；Runtime 会先撤销其运行态可见性。"""
        del spec, generation
        instance.stop()

    @staticmethod
    def cleanup(
        spec: CapabilitySpec,
        candidate: Any,
        generation: int,
        error: BaseException,
    ) -> None:
        """启动失败后尽力回收候选实例已创建的部分资源。"""
        del spec, generation, error
        candidate.stop()
