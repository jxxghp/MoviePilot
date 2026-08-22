from __future__ import annotations

import collections.abc
import importlib
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Optional

from app.runtime.capabilities.model import (
    ActivationPolicy,
    AdapterExecutionMode,
    CapabilitySpec,
    SelectorSchema,
)
from app.runtime.capabilities.registry import CapabilityRegistry
from app.runtime.config import settings
from app.runtime.extensions.contract.extension import (
    ExtensionDistribution,
    ExtensionFaultScope,
    ExtensionProvider,
    extension_capability_names,
    is_implemented_callable,
    supports_extension_hook,
)
from app.runtime.extensions.service_config import (
    service_capability,
    service_capability_configs,
    service_config_key,
)
from app.runtime.log import logger
from app.schemas.media import normalize_media_source
from app.schemas.types import ModuleType


HOST_MODULE_KIND = "host_module"
_SETTING_SELECTOR = "setting_truthy"
_SERVICE_SELECTOR = "system_config_item"
_MODULE_ROOT = Path(__file__).resolve().parents[3] / "modules"
# 模块 manifest 必须声明的元数据字段
_REQUIRED_METADATA_FIELDS = frozenset({"name", "priority"})
# 只有按服务配置扇出多实例的模块才声明的元数据字段，取值是服务能力标签，
# 与扩展声明服务实例时用的是同一套取值；该族配置存放在哪个 systemconfig 键
# 由宿主内部对照，manifest 不重复声明
_SERVICE_CAPABILITY_FIELD = "service_capability"
# subtype 是可选元数据字段，声明时必须是非空字符串；渠道标识的取值不
# 再要求登记于内核枚举，是否声明由模块自身决定
_SUBTYPE_FIELD = "subtype"
# 本模块承载的服务实例类型声明。声明的是「用户在配置列表里配的那个类型是谁、能配几份」，
# 与 service_capability 回答的「本模块是不是该族的实例持有者」不是同一件事：存储实例按
# 令牌由存储后端注册表寻址、不走 get_instances()，因此存储模块只有本表而没有前者。
_SERVICE_INSTANCE_FIELD = "service_instance"
_SERVICE_INSTANCE_KEYS = frozenset({"capability", "type", "multi_instance"})
# 本模块服务哪些媒体数据源。一个模块可以服务多个来源，故取值是标识数组。
_MEDIA_SOURCES_FIELD = "media_sources"
_OPTIONAL_METADATA_FIELDS = frozenset({
    _SERVICE_CAPABILITY_FIELD,
    _SUBTYPE_FIELD,
    _SERVICE_INSTANCE_FIELD,
    _MEDIA_SOURCES_FIELD,
})
_NOTIFICATION_CAPABILITY = ModuleType.Notification.value


def service_instance_declaration(
    spec: CapabilitySpec,
) -> Optional[tuple[str, str, bool]]:
    """读出模块 manifest 里的服务实例类型声明。

    ``capability`` 与 ``type`` 允许省略：前者省略时取 ``metadata.service_capability``，
    后者省略时取 ``system_config_item`` selector 的 ``match_value``——两处都是同一份
    manifest 里已经写下的同一个事实，重复写一遍只会多一个漂移点；同时给出时由
    `_validate_manifest_inventory` 判定两者一致。``multi_instance`` 省略时为多实例，
    与声明面的缺省一致，因此不写该表的模块行为不变。

    :param spec: 模块声明
    :return: (能力标签, 类型标识, 能否配多份)；未声明服务实例类型时为 None
    """
    declared = spec.metadata.get(_SERVICE_INSTANCE_FIELD)
    if not isinstance(declared, collections.abc.Mapping):
        return None
    capability = declared.get("capability") or spec.metadata.get(_SERVICE_CAPABILITY_FIELD)
    service_type = declared.get("type")
    if service_type is None and spec.selector is not None:
        if spec.selector.kind == _SERVICE_SELECTOR:
            service_type = spec.selector.config.get("match_value")
    if not isinstance(capability, str) or not isinstance(service_type, str):
        return None
    multi_instance = declared.get("multi_instance", True)
    if not isinstance(multi_instance, bool):
        return None
    return capability, service_type, multi_instance


def declared_media_sources(spec: CapabilitySpec) -> tuple[str, ...]:
    """读出模块 manifest 里声明服务的媒体数据源标识。

    :param spec: 模块声明
    :return: 规范化后的来源标识元组，按声明顺序排列；未声明时为空元组
    """
    declared = spec.metadata.get(_MEDIA_SOURCES_FIELD)
    if not isinstance(declared, (tuple, list)):
        return ()
    normalized = tuple(
        str(source)
        for item in declared
        if (source := normalize_media_source(item)) is not None
    )
    return normalized


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
    if service_capability(key) is None:
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


def _validate_service_instance_metadata(
    spec: CapabilitySpec,
    capability: Optional[Any],
) -> Optional[tuple[str, str]]:
    """校验模块 manifest 的服务实例类型声明，并给出其坐标。

    ``capability`` 与 ``type`` 省略时各自回落到同一份 manifest 里已有的事实，因此
    这两个键只在回落取不到值时必填；显式给出且与回落值不一致的写法一律拒绝——同一
    个事实在一份文件里写出两个取值，无论宿主取哪一个都会与另一个悖离。

    :param spec: 模块声明
    :param capability: 该模块 metadata 里的 service_capability 取值，未声明时为 None
    :return: (能力标签, 类型标识)；未声明服务实例类型时为 None
    :raises ValueError: 声明形状非法、取值不属于任何服务族，或与既有事实冲突
    """
    declared = spec.metadata.get(_SERVICE_INSTANCE_FIELD)
    if declared is None:
        return None
    if not isinstance(declared, collections.abc.Mapping):
        raise ValueError(f"{spec.source}: metadata.service_instance 必须是 table")
    unknown_keys = set(declared) - _SERVICE_INSTANCE_KEYS
    if unknown_keys:
        raise ValueError(
            f"{spec.source}: metadata.service_instance 未知字段 {sorted(unknown_keys)}"
        )
    declared_capability = declared.get("capability")
    if declared_capability is not None and declared_capability != capability and capability is not None:
        raise ValueError(
            f"{spec.source}: metadata.service_instance.capability="
            f"{declared_capability!r} 与 metadata.service_capability={capability!r} 不一致"
        )
    resolved_capability = declared_capability if declared_capability is not None else capability
    if not isinstance(resolved_capability, str) or service_config_key(resolved_capability) is None:
        raise ValueError(
            f"{spec.source}: 非法 metadata.service_instance.capability={resolved_capability!r}"
        )
    selector_type = (
        spec.selector.config.get("match_value")
        if spec.selector is not None and spec.selector.kind == _SERVICE_SELECTOR
        else None
    )
    declared_type = declared.get("type")
    if declared_type is not None and selector_type is not None and declared_type != selector_type:
        raise ValueError(
            f"{spec.source}: metadata.service_instance.type={declared_type!r} 与 selector "
            f"的 match_value={selector_type!r} 不一致"
        )
    resolved_type = declared_type if declared_type is not None else selector_type
    if not isinstance(resolved_type, str) or not resolved_type:
        raise ValueError(
            f"{spec.source}: metadata.service_instance.type 必须是非空字符串"
        )
    multi_instance = declared.get("multi_instance", True)
    if not isinstance(multi_instance, bool):
        raise ValueError(
            f"{spec.source}: metadata.service_instance.multi_instance="
            f"{multi_instance!r} 不是布尔值，无法判定该类型能配几份"
        )
    config_key = service_config_key(resolved_capability)
    if config_key.value not in spec.watch:
        raise ValueError(
            f"{spec.source}: activation.watch 必须包含 "
            f"metadata.service_instance.capability 对应的服务配置键"
        )
    return resolved_capability, resolved_type


def _validate_media_sources_metadata(spec: CapabilitySpec) -> tuple[str, ...]:
    """校验模块 manifest 声明服务的媒体数据源标识。

    :param spec: 模块声明
    :return: 规范化后的来源标识元组；未声明时为空元组
    :raises ValueError: 声明形状非法、含非法来源标识或含重复项
    """
    declared = spec.metadata.get(_MEDIA_SOURCES_FIELD)
    if declared is None:
        return ()
    if not isinstance(declared, (tuple, list)):
        raise ValueError(f"{spec.source}: metadata.media_sources 必须是字符串数组")
    normalized: list[str] = []
    for item in declared:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(
                f"{spec.source}: metadata.media_sources 只能包含非空字符串"
            )
        source = normalize_media_source(item)
        if source is None:
            raise ValueError(
                f"{spec.source}: metadata.media_sources 含非法来源标识 {item!r}"
            )
        normalized.append(str(source))
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"{spec.source}: metadata.media_sources 不能包含重复来源")
    return tuple(normalized)


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

    # 「一个服务实例类型由谁提供」与「一个媒体来源由谁服务」都必须是单值：两个模块认领
    # 同一项时，取哪一个都只能靠遍历先后决定，正是声明面要消灭的形态。
    service_instance_owners: dict[tuple[str, str], Path] = {}
    media_source_owners: dict[str, Path] = {}
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
            - _OPTIONAL_METADATA_FIELDS
        )
        if missing_metadata or unknown_metadata:
            raise ValueError(
                f"{spec.source}: metadata 字段非法，"
                f"missing={sorted(missing_metadata)} unknown={sorted(unknown_metadata)}"
            )
        capability = spec.metadata.get(_SERVICE_CAPABILITY_FIELD)
        if capability is not None:
            config_key = service_config_key(capability)
            if config_key is None:
                raise ValueError(
                    f"{spec.source}: 非法 metadata.service_capability={capability!r}"
                )
            if config_key.value not in spec.watch:
                raise ValueError(
                    f"{spec.source}: activation.watch 必须包含 "
                    f"metadata.service_capability 对应的服务配置键"
                )
        coordinate = _validate_service_instance_metadata(spec, capability)
        if coordinate is not None:
            owner = service_instance_owners.get(coordinate)
            if owner is not None:
                raise ValueError(
                    f"{spec.source}: 服务实例类型 {coordinate} 已由 {owner} 声明"
                )
            service_instance_owners[coordinate] = spec.source
        for media_source in _validate_media_sources_metadata(spec):
            owner = media_source_owners.get(media_source)
            if owner is not None:
                raise ValueError(
                    f"{spec.source}: 媒体数据源 {media_source!r} 已由 {owner} 声明"
                )
            media_source_owners[media_source] = spec.source
        subtype = spec.metadata.get(_SUBTYPE_FIELD)
        if subtype is not None and (not isinstance(subtype, str) or not subtype):
            raise ValueError(f"{spec.source}: metadata.subtype 必须是非空字符串")
        if capability == _NOTIFICATION_CAPABILITY and subtype is None:
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


def host_module_root() -> Path:
    """返回内建模块声明的发现根。

    :return: 一级模块包所在目录
    """
    return _MODULE_ROOT


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
        key: tuple(service_capability_configs(service_capability(key)))
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
