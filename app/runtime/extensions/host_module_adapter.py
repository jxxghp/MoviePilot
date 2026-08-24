from __future__ import annotations

import importlib
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from app.runtime.capabilities.model import (
    ActivationPolicy,
    AdapterExecutionMode,
    CapabilitySpec,
    SelectorSchema,
)
from app.runtime.capabilities.registry import CapabilityRegistry
from app.runtime.settings import RuntimeSettingsCompat

settings = RuntimeSettingsCompat()
from app.runtime.extensions.service_config import ServiceConfigHelper
from app.schemas.types import (
    DownloaderType,
    MediaRecognizeType,
    MediaServerType,
    NotificationChannel,
    ModuleType,
    OtherModulesType,
    StorageSchema,
    SystemConfigKey,
)


HOST_MODULE_KIND = "host_module"
_SETTING_SELECTOR = "setting_truthy"
_SERVICE_SELECTOR = "system_config_item"
_MODULE_ROOT = Path(__file__).resolve().parents[2] / "modules"
_SERVICE_CONFIG_GETTERS = MappingProxyType({
    SystemConfigKey.Downloaders.value: ServiceConfigHelper.get_downloader_configs,
    SystemConfigKey.MediaServers.value: ServiceConfigHelper.get_mediaserver_configs,
    SystemConfigKey.Notifications.value: ServiceConfigHelper.get_notification_configs,
})
_SUBTYPE_NAMES = frozenset(
    item.name
    for enum_type in (
        DownloaderType,
        MediaServerType,
        NotificationChannel,
        StorageSchema,
        OtherModulesType,
        MediaRecognizeType,
    )
    for item in enum_type
)


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
    """校验一级模块包与 manifest 一一对应，并固定宿主声明合同。"""
    module_packages = {
        child.name
        for child in _MODULE_ROOT.iterdir()
        if child.is_dir()
        and not child.name.startswith("_")
        and (child / "__init__.py").is_file()
    }
    specs = registry.list_specs()
    manifest_packages = {spec.source.parent.name for spec in specs}
    if module_packages != manifest_packages:
        missing = sorted(module_packages - manifest_packages)
        unknown = sorted(manifest_packages - module_packages)
        raise ValueError(
            f"Host Module manifest inventory 不一致：missing={missing} unknown={unknown}"
        )

    allowed_metadata = {"name", "type", "subtype", "priority"}
    module_type_values = {item.value for item in ModuleType}
    for spec in specs:
        if spec.source.parent.parent != _MODULE_ROOT:
            raise ValueError(f"Host Module manifest 必须位于一级模块包：{spec.source}")
        module_name, symbol_name = spec.entrypoint.split(":", maxsplit=1)
        expected_module = f"app.modules.{spec.source.parent.name}"
        if module_name != expected_module or symbol_name != spec.id:
            raise ValueError(
                f"{spec.source}: entrypoint 必须指向同包且类名等于 capability id"
            )
        if set(spec.metadata) != allowed_metadata:
            raise ValueError(
                f"{spec.source}: metadata 字段必须是 {sorted(allowed_metadata)}"
            )
        if spec.metadata["type"] not in module_type_values:
            raise ValueError(f"{spec.source}: 非法 metadata.type={spec.metadata['type']!r}")
        if spec.metadata["subtype"] not in _SUBTYPE_NAMES:
            raise ValueError(
                f"{spec.source}: 非法 metadata.subtype={spec.metadata['subtype']!r}"
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


def build_host_module_registry() -> CapabilityRegistry:
    """从现有物理模块包构建 import-free Host Module Registry。"""
    registry = CapabilityRegistry.discover(
        (_MODULE_ROOT,),
        kinds={HOST_MODULE_KIND},
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
        del generation
        if instance.stop() is False:
            raise RuntimeError(f"Host Module {spec.id} 资源未完成收口")

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
