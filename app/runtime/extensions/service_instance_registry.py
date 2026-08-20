"""服务实例注册表：按「服务配置键加类型标识」登记扩展提供的服务实例类型。

宿主的服务发现按配置扇出实例——同一类型下用户配置了几条，就有几个具名实例。
内建模块靠 `capability.toml` 的 ``service_config`` 声明归属并自持实例；扩展声明
的类型不进入模块清单，改由本表为每条声明持有一个适配器，适配器实现与内建模块
同名的 ``get_instances()``，从而与内建模块一起被服务发现取用。

登记由扩展实例的生命周期驱动：实例启动或配置生效时按当前声明重建，实例停止时
按登记方回收。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from app.runtime.extensions.contract import ExtensionDistribution
from app.runtime.extensions.service_config import ServiceConfigHelper
from app.runtime.log import logger
from app.schemas.types import SystemConfigKey

# 服务配置键到配置读取端口的映射，配置一律经既有端口取用并已通过 Schema 校验
SERVICE_CONFIG_READERS: Mapping[str, Callable[[], List[Any]]] = MappingProxyType({
    SystemConfigKey.Downloaders.value: ServiceConfigHelper.get_downloader_configs,
    SystemConfigKey.MediaServers.value: ServiceConfigHelper.get_mediaserver_configs,
    SystemConfigKey.Notifications.value: ServiceConfigHelper.get_notification_configs,
})


@dataclass(frozen=True, slots=True)
class ServiceInstanceEntry:
    """服务实例类型在注册表中的一条登记。

    配置界面二选一：``config_form`` 为 vuetify 模式，``config_component`` 为 vue
    模式的已解析组件描述（组件名加联邦远程入口）；未声明界面时二者均为 None，
    此时前端沿用内建类型的渲染方式，不视为异常。

    :param config_key: 服务配置键
    :param service_type: 类型标识，与该族配置模型的 ``type`` 字段取值对应
    :param name: 类型展示名称
    :param impl: 实例实现类，按 ``impl(name=..., **config)`` 构造
    :param distribution: 提供方的发行方式
    :param owner: 提供该类型的扩展实例键
    :param config_form: 登记方为该类型声明的专属配置界面，形状为
        (组件树, 默认数据) 二元组
    :param config_component: 登记方为该类型声明的 vue 模式配置组件，形状为
        ``{"component": 组件名, "remote": 联邦远程入口描述}``
    """

    config_key: str
    service_type: str
    name: str
    impl: Any
    distribution: ExtensionDistribution
    owner: str
    config_form: Optional[Tuple[List[Dict[str, Any]], Dict[str, Any]]] = None
    config_component: Optional[Dict[str, Any]] = None


class ServiceInstanceAdapter:
    """把一条服务实例类型登记投影为服务发现可取用的实例持有者。

    服务发现每次取服务都会调用 ``get_instances()``，因此实例按配置缓存：配置未变
    的实例原样复用，配置变化的实例重建，配置消失的实例摘除。这与内建模块在配置
    事件里重建实例的效果一致，区别只是重建时机由取用触发而非由配置事件触发。
    """

    def __init__(self, entry: ServiceInstanceEntry) -> None:
        """绑定登记项并建立空实例缓存。

        :param entry: 服务实例类型登记项
        """
        self._entry = entry
        self._lock = threading.RLock()
        self._configs: Dict[str, Any] = {}
        self._instances: Dict[str, Any] = {}
        # 最近一次构造失败的配置，用于让同一条坏配置只报错一次而不是每次取服务都刷屏
        self._failed_configs: Dict[str, Any] = {}

    @property
    def entry(self) -> ServiceInstanceEntry:
        """返回本适配器承载的登记项。"""
        return self._entry

    def get_name(self) -> str:
        """返回类型展示名称，与内建模块的同名方法语义一致。

        :return: 类型展示名称
        """
        return self._entry.name

    def get_instances(self) -> Dict[str, Any]:
        """按当前用户配置返回本类型的全部具名实例。

        单条配置构造失败只跳过它自己，同一类型下其余配置照常产出；读取配置整体
        出错时返回空字典，不向上抛，避免一个扩展的登记击穿整族服务发现。

        :return: 实例名到实例的映射
        """
        desired = self._desired_configs()
        with self._lock:
            for name in [name for name in self._instances if name not in desired]:
                self._instances.pop(name, None)
                self._configs.pop(name, None)
            for name in [name for name in self._failed_configs if name not in desired]:
                self._failed_configs.pop(name, None)
            for name, conf in desired.items():
                if name in self._instances and self._configs.get(name) == conf:
                    continue
                instance = self._create_instance(name, conf)
                if instance is None:
                    self._instances.pop(name, None)
                    self._configs.pop(name, None)
                    continue
                self._instances[name] = instance
                self._configs[name] = conf
                self._failed_configs.pop(name, None)
            return dict(self._instances)

    def _desired_configs(self) -> Dict[str, Any]:
        """读取本类型下已启用的用户配置。

        :return: 实例名到配置的映射；配置读取出错时为空字典
        """
        reader = SERVICE_CONFIG_READERS.get(self._entry.config_key)
        if reader is None:
            return {}
        try:
            configs = reader() or []
        except Exception as error:
            logger.error(
                f"【服务】读取 {self._entry.config_key} 配置出错，"
                f"扩展 {self._entry.owner} 声明的 {self._entry.service_type} 实例暂不可用：{error}"
            )
            return {}
        return {
            conf.name: conf
            for conf in configs
            if conf.name and conf.type == self._entry.service_type and conf.enabled
        }

    def _create_instance(self, name: str, conf: Any) -> Optional[Any]:
        """按单条配置构造一个具名实例。

        :param name: 实例名
        :param conf: 该实例的用户配置
        :return: 实例；构造失败时为 None
        """
        try:
            return self._entry.impl(name=name, **(conf.config or {}))
        except Exception as error:
            if self._failed_configs.get(name) != conf:
                self._failed_configs[name] = conf
                logger.error(
                    f"【服务】扩展 {self._entry.owner} 声明的 {self._entry.config_key} "
                    f"类型 {self._entry.service_type} 实例 {name} 构造失败，已跳过：{error}"
                )
            return None


class ServiceInstanceRegistry:
    """按「服务配置键加类型标识」登记扩展提供的服务实例类型。"""

    def __init__(self) -> None:
        """创建登记表。"""
        self._lock = threading.RLock()
        self._adapters: dict[tuple[str, str], ServiceInstanceAdapter] = {}

    def register(self,
                 config_key: str,
                 service_type: str,
                 name: str,
                 impl: Any,
                 owner: str,
                 distribution: ExtensionDistribution = ExtensionDistribution.MARKET,
                 config_form: Optional[Tuple[List[Dict[str, Any]], Dict[str, Any]]] = None,
                 config_component: Optional[Dict[str, Any]] = None
                 ) -> Optional[str]:
        """登记一个服务实例类型，同「配置键加类型」重复登记以最新一次为准。

        登记内容与既有登记完全相同时保留原适配器，使已构造的实例不因一次无变化的
        重新同步而全部重建。

        :param config_key: 服务配置键
        :param service_type: 类型标识
        :param name: 类型展示名称
        :param impl: 实例实现类
        :param owner: 提供该类型的扩展实例键
        :param distribution: 提供方的发行方式
        :param config_form: 该类型的专属配置界面（vuetify 模式）
        :param config_component: 该类型的已解析 vue 模式配置组件
        :return: 登记成功的类型标识；配置键或类型标识缺失时为 None
        """
        if not config_key or not service_type:
            logger.error(f"【服务】{owner} 的服务实例声明缺少配置键或类型标识，无法登记")
            return None
        entry = ServiceInstanceEntry(
            config_key=config_key,
            service_type=service_type,
            name=name,
            impl=impl,
            distribution=distribution,
            owner=owner,
            config_form=config_form,
            config_component=config_component,
        )
        with self._lock:
            existing = self._adapters.get((config_key, service_type))
            if existing is None or existing.entry != entry:
                self._adapters[(config_key, service_type)] = ServiceInstanceAdapter(entry)
        logger.info(f"【服务】{owner} 提供 {config_key} 类型 {service_type}（{name}）")
        return service_type

    def unregister_owner(self, owner: str) -> Tuple[str, ...]:
        """注销指定登记方当前仍生效的全部服务实例类型。

        类型一旦被更晚的登记覆盖，owner 随之更新为新的登记方，因此本方法只回收
        当前仍归属该登记方的条目，不会波及后来居上、已接管同一类型的登记方。

        :param owner: 登记方标识
        :return: 被注销的类型标识元组
        """
        with self._lock:
            owned = tuple(
                key for key, adapter in self._adapters.items()
                if adapter.entry.owner == owner
            )
            for key in owned:
                self._adapters.pop(key, None)
            return tuple(service_type for _config_key, service_type in owned)

    def adapters(self, config_key: str) -> Tuple[ServiceInstanceAdapter, ...]:
        """列出指定服务配置键下当前登记的全部适配器。

        :param config_key: 服务配置键
        :return: 适配器元组，按登记顺序排列
        """
        if not config_key:
            return ()
        with self._lock:
            return tuple(
                adapter for (key, _service_type), adapter in self._adapters.items()
                if key == config_key
            )

    def find(self, config_key: str, service_type: str) -> Optional[ServiceInstanceEntry]:
        """查找指定「配置键加类型标识」的登记项。

        :param config_key: 服务配置键
        :param service_type: 类型标识
        :return: 登记项；未登记时为 None
        """
        if not config_key or not service_type:
            return None
        with self._lock:
            adapter = self._adapters.get((config_key, service_type))
        return adapter.entry if adapter else None

    def entries(self) -> Tuple[ServiceInstanceEntry, ...]:
        """列出当前登记的全部条目。

        :return: 登记项元组，按登记顺序排列
        """
        with self._lock:
            return tuple(adapter.entry for adapter in self._adapters.values())

    def diagnose(self) -> list[dict[str, Any]]:
        """输出只读的登记诊断信息。

        :return: 每个类型的配置键、类型标识、发行方式与提供方
        """
        return [
            {
                "config_key": entry.config_key,
                "type": entry.service_type,
                "distribution": entry.distribution.value,
                "owner": entry.owner,
            }
            for entry in self.entries()
        ]


service_instance_registry = ServiceInstanceRegistry()
