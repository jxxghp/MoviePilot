from __future__ import annotations

import sys
import threading
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional, Tuple

from app.foundation.singleton import Singleton
from app.runtime.capabilities.model import (
    CapabilityLifecycleState,
    CapabilityObservation,
    CapabilitySpec,
)
from app.runtime.capabilities.registry import CAPABILITY_MANIFEST_NAME
from app.runtime.capabilities.runtime import CapabilityRuntime
from app.runtime.config import settings
from app.runtime.events import Event, EventHandlerBinding, eventmanager
from app.runtime.extensions.contract import supports_extension_hook
from app.runtime.extensions.host_module_adapter import (
    HOST_MODULE_KIND,
    PLUGIN_MODULE_KIND,
    HostModuleAdapter,
    HostModuleExtension,
    build_host_module_registry,
    capture_host_module_config,
    should_run_host_module,
)
from app.runtime.log import logger
from app.schemas.types import EventType


def _no_plugin_capability_roots() -> tuple:
    """未接入扩展声明根时不追加任何发现根。"""
    return ()


_plugin_capability_roots = _no_plugin_capability_roots


def configure_plugin_capability_roots(provider) -> None:
    """
    由启动组合根注入扩展能力声明根的来源

    扩展的声明根取自各扩展当前生效版本的源码目录，判定哪些扩展已安装、生效在哪个
    版本需要读持久化配置，运行时层不得反向依赖数据库，因此只声明可注入的提供者。
    :param provider: 无参可调用对象，返回声明根的可迭代集合；传空恢复为不追加
    """
    global _plugin_capability_roots
    _plugin_capability_roots = provider or _no_plugin_capability_roots


class ModuleManager(metaclass=Singleton):
    """以 Capability Runtime 管理宿主模块与扩展模块，并保留旧插件同步查询合同。"""

    def __init__(self) -> None:
        """发现 data-only manifest，并按当前配置激活所需模块。"""
        self._lock = threading.RLock()
        self._lifecycle_lock = threading.RLock()
        self._modules: dict[str, type] = {}
        self._running_modules: dict[str, Any] = {}
        # 发布视图代际；每次运行模块投影刷新都自增，用于作废能力索引缓存。
        self._running_generation = 0
        self._capability_index: Optional[dict[str, tuple[Any, ...]]] = None
        self._capability_index_generation = -1
        registry = self._build_registry(self._discover_plugin_capability_roots())
        # 扩展模块与宿主模块共用适配器：物化走标准 import 与属性取值，对扩展源码
        # 目录下的入口同样成立，两者的创建、启动、停止语义因此完全一致。
        adapter = HostModuleAdapter()
        self._runtime = CapabilityRuntime(
            registry,
            adapters={HOST_MODULE_KIND: adapter, PLUGIN_MODULE_KIND: adapter},
            observer=self._observe_transition,
        )
        # pkgutil 的既有发现顺序按一级包名稳定排列，兼容视图继续保持该顺序。
        self._specs = tuple(
            sorted(self._runtime.list_specs(), key=lambda item: item.source.parent.name)
        )
        eventmanager.register_handler_instance_resolver(
            "modules",
            self.resolve_event_handler_instance,
        )
        eventmanager.add_event_listener(
            EventType.ConfigChanged,
            self.handle_config_changed,
        )
        self.load_modules()

    @staticmethod
    def _discover_plugin_capability_roots() -> tuple:
        """
        取扩展提供的能力声明根，来源出错时按无扩展处理

        只保留确实存在且带能力清单的目录：绝大多数扩展只注册事件或提供业务钩子，
        目录下没有清单，把它们当成声明根会让发现流程因「声明根没有清单」直接失败，
        一个传统扩展就能让宿主模块整体装不起来。
        :return: 声明根元组，取不到时为空元组
        """
        try:
            roots = tuple(_plugin_capability_roots() or ())
        except Exception as err:
            logger.error(f"读取扩展能力声明根出错，本次按无扩展处理：{str(err)}")
            return ()
        return tuple(
            root
            for root in roots
            if root and Path(root).is_dir() and any(Path(root).rglob(CAPABILITY_MANIFEST_NAME))
        )

    @staticmethod
    def _build_registry(plugin_roots: tuple):
        """
        构建模块注册表，坏的扩展声明只连累它自己

        先逐个声明根单独试建：清单非法、入口不可解析等问题出在哪个扩展就跳过哪个，
        其余扩展照常装载。全部合并时仍可能因跨扩展的 capability id 重名失败，此时
        退回只含宿主模块的注册表——宿主不能因为扩展之间的冲突起不来。
        :param plugin_roots: 扩展能力声明根
        :return: 能力注册表
        """
        if not plugin_roots:
            return build_host_module_registry()
        usable = []
        for root in plugin_roots:
            try:
                build_host_module_registry((root,))
            except Exception as err:
                logger.error(f"扩展能力声明有误，已跳过 {root}：{str(err)}")
                continue
            usable.append(root)
        try:
            return build_host_module_registry(tuple(usable))
        except Exception as err:
            logger.error(f"扩展能力声明相互冲突，本次只装载宿主模块：{str(err)}")
            return build_host_module_registry()

    @staticmethod
    def _observe_transition(observation: CapabilityObservation) -> None:
        """把 Runtime 的稳定转换结果接入现有日志面。"""
        if observation.outcome == "failed":
            logger.error(
                "Host Module %s %s 失败：%s",
                observation.capability_id,
                observation.operation,
                observation.error,
            )
        elif observation.outcome == "succeeded":
            logger.debug(
                "Host Module %s %s 完成，generation=%s，耗时=%.2fms",
                observation.capability_id,
                observation.operation,
                observation.generation,
                observation.duration_ms,
            )

    @staticmethod
    def _event_changed_keys(event: Optional[Event]) -> set[str]:
        """兼容对象和 dict 两种配置事件载荷。"""
        if not event:
            return set()
        event_data = event.event_data
        if isinstance(event_data, dict):
            keys = event_data.get("key", set())
        else:
            keys = getattr(event_data, "key", set())
        if isinstance(keys, str):
            return {keys}
        return {str(key) for key in (keys or set())}

    def _remember_materialized(self, module_id: str, implementation: type) -> type:
        """更新旧 `_modules` 视图，但不改变能力资源生命周期。"""
        with self._lock:
            self._modules[module_id] = implementation
        return implementation

    def _consumer_materialized_class(self, spec: CapabilitySpec) -> Optional[type]:
        """识别插件显式旧导入产生的真实类，不触发新的 Python import。"""
        module_name, symbol_name = spec.entrypoint.split(":", maxsplit=1)
        module = sys.modules.get(module_name)
        namespace = getattr(module, "__dict__", None) if module is not None else None
        if not isinstance(namespace, dict):
            return None
        implementation = namespace.get(symbol_name)
        return implementation if isinstance(implementation, type) else None

    def _refresh_running_projection(self) -> None:
        """从 Runtime 已发布实例重建插件可见的运行模块字典。"""
        running = {
            spec.id: instance
            for spec in self._specs
            if (instance := self._runtime.get_running(spec.id)) is not None
        }
        with self._lock:
            self._running_modules = running
            self._running_generation += 1
            self._capability_index = None
            self._capability_index_generation = -1

    def resolve_event_handler_instance(
        self,
        owner_class: type,
    ) -> Optional[list[EventHandlerBinding]]:
        """按 canonical class identity 绑定当前 generation，停止态阻断 fallback 构造。"""
        for spec in self._specs:
            with self._lock:
                implementation = self._modules.get(spec.id)
            if implementation is None:
                implementation = self._consumer_materialized_class(spec)
                if implementation is not None:
                    self._remember_materialized(spec.id, implementation)
                    # 同步 Runtime 的物化观测，但不创建或启动实例。
                    self._runtime.snapshot(spec.id)
            if implementation is not owner_class:
                continue
            return [
                EventHandlerBinding(
                    instance=self._runtime.get_running(spec.id),
                    owner_name=str(spec.metadata["name"]),
                )
            ]
        return None

    def _reconcile(
        self,
        *,
        reason: str,
        changed_keys: Optional[set[str]] = None,
        reload_running: bool = False,
    ) -> None:
        """以一次配置快照串行协调需要启动、重载或停止的能力。"""
        with self._lifecycle_lock:
            selected = tuple(
                spec
                for spec in self._specs
                if changed_keys is None or changed_keys.intersection(spec.watch)
            )
            snapshot = capture_host_module_config(selected)
            for spec in selected:
                desired = should_run_host_module(spec, snapshot)
                running = self._runtime.get_running(spec.id)
                try:
                    if desired and running is None:
                        instance = self._runtime.activate(
                            spec.id,
                            reason=reason,
                            retry=True,
                        )
                        self._remember_materialized(spec.id, type(instance))
                    elif desired and running is not None and reload_running:
                        instance = self._runtime.reload(spec.id, reason=reason)
                        self._remember_materialized(spec.id, type(instance))
                    elif not desired and running is not None:
                        self._runtime.stop(spec.id, reason=reason)
                except Exception:
                    # 单能力失败由 Runtime 完整记录；其它无依赖能力继续 reconcile。
                    continue
            self._refresh_running_projection()

    def load_modules(self) -> None:
        """按当前配置启动未运行模块；已运行模块保持当前 generation。"""
        self._reconcile(reason="module_manager_load")

    def handle_config_changed(self, event: Event) -> None:
        """配置变更时仅协调 watch 命中的能力，并保证单一生命周期 writer。"""
        changed_keys = self._event_changed_keys(event)
        if not changed_keys:
            return
        self._reconcile(
            reason="config_changed",
            changed_keys=changed_keys,
            reload_running=True,
        )

    def stop(self) -> None:
        """停止全部运行模块但保留 Runtime，使旧插件可随后再次 load。"""
        logger.info("正在停止所有模块...")
        with self._lifecycle_lock:
            for spec in reversed(self._specs):
                snapshot = self._runtime.snapshot(spec.id)
                if (
                    self._runtime.get_running(spec.id) is None
                    and snapshot.lifecycle is not CapabilityLifecycleState.FAILED
                ):
                    continue
                try:
                    self._runtime.stop(spec.id, reason="module_manager_stop")
                except Exception:
                    continue
            self._refresh_running_projection()
        logger.info("所有模块停止完成")

    def shutdown(self) -> None:
        """进程关闭时不可逆停止 Runtime，阻止并发能力重新发布。"""
        logger.info("正在关闭模块运行时...")
        with self._lifecycle_lock:
            self._runtime.shutdown(reason="application_shutdown")
            self._refresh_running_projection()
        logger.info("模块运行时关闭完成")

    def reload(self) -> None:
        """保留旧插件可观察的 stop、load、ModuleReload 同步顺序。"""
        with self._lifecycle_lock:
            self.stop()
            self.load_modules()
            eventmanager.send_event(etype=EventType.ModuleReload, data={})

    def test(self, modleid: str) -> Tuple[bool, str]:
        """测试已运行模块；未启用模块保持旧合同返回 `(False, "")`。"""
        module = self.get_running_module(modleid)
        if module is None:
            return False, ""
        extension = HostModuleExtension(module)
        if not extension.supports_hook("test"):
            return True, "模块不支持测试"
        result = extension.self_test()
        return result if result else (False, "")

    @staticmethod
    def check_setting(setting: Optional[tuple]) -> bool:
        """保留旧模块开关的 truthy 与 membership 判定语义。"""
        if not setting:
            return True
        switch, value = setting
        option = getattr(settings, switch)
        if not option:
            return False
        if value is True:
            return True
        return value in option

    def get_running_module(self, module_id: str) -> Any:
        """根据模块 ID 返回已发布的运行实例，不触发物化。"""
        if not module_id or self._runtime.get_spec(module_id) is None:
            return None
        return self._runtime.get_running(module_id)

    def _running_snapshot(self) -> tuple[Any, ...]:
        """直接读取 Runtime 发布视图，转换期间不暴露旧或候选实例。"""
        return tuple(
            instance
            for spec in self._specs
            if (instance := self._runtime.get_running(spec.id)) is not None
        )

    def get_running_modules(self, method: str) -> Generator:
        """返回实现了指定方法的运行模块快照。"""
        for module in self._running_snapshot():
            if supports_extension_hook(module, method):
                yield module

    def _build_capability_index(self) -> dict[str, tuple[Any, ...]]:
        """按扩展契约取用运行实例的已实现方法，构建方法名到提供者的索引。

        :return: 方法名到按优先级升序排列的提供者元组的映射
        """
        collected: dict[str, list[HostModuleExtension]] = {}
        for module in self._running_snapshot():
            extension = HostModuleExtension(module)
            for name in extension.capability_names():
                collected.setdefault(name, []).append(extension)
        return {
            name: tuple(
                extension.instance
                for extension in sorted(
                    extensions,
                    key=lambda item: item.priority,
                )
            )
            for name, extensions in collected.items()
        }

    def _capability_index_snapshot(self) -> dict[str, tuple[Any, ...]]:
        """返回与当前发布代际一致的能力索引，必要时重建缓存。

        :return: 方法名到按优先级升序排列的提供者元组的映射
        """
        with self._lock:
            index = self._capability_index
            if (
                index is not None
                and self._capability_index_generation == self._running_generation
            ):
                return index
            generation = self._running_generation
        # 反射开销不进锁，避免长时间阻塞生命周期写入方。
        rebuilt = self._build_capability_index()
        with self._lock:
            if generation == self._running_generation:
                self._capability_index = rebuilt
                self._capability_index_generation = generation
        return rebuilt

    def providers_for(self, method: str) -> tuple[Any, ...]:
        """返回实现了指定方法的运行模块，按 `get_priority()` 升序排列。

        :param method: 模块方法名称
        :return: 提供该方法的运行模块元组；无提供者时为空元组
        """
        if not method:
            return ()
        return self._capability_index_snapshot().get(method, ())

    def get_service_config_modules(self, config_key: str) -> Generator:
        """返回 manifest 声明消费指定服务配置键的运行模块快照。

        :param config_key: 服务配置键，取值为 `SystemConfigKey` 的成员值
        :return: 运行模块迭代器，按 manifest 发现顺序产出
        """
        if not config_key:
            return
        for spec in self._specs:
            if spec.metadata.get("service_config") != config_key:
                continue
            instance = self._runtime.get_running(spec.id)
            if instance is not None:
                yield instance

    def get_module(self, module_id: str) -> Any:
        """显式物化并返回 canonical 模块类；失败保持旧合同返回 None。"""
        if not module_id or self._runtime.get_spec(module_id) is None:
            return None
        with self._lock:
            implementation = self._modules.get(module_id)
        if implementation is not None:
            return implementation
        try:
            implementation = self._runtime.materialize(
                module_id,
                reason="compat_get_module",
                retry=True,
            )
        except Exception:
            return None
        return self._remember_materialized(module_id, implementation)

    def get_modules(self) -> dict[str, type]:
        """兼容性显式物化全部真实类；单个失败不阻断其它模块。"""
        for spec in self._specs:
            self.get_module(spec.id)
        with self._lock:
            return dict(self._modules)

    def get_module_ids(self) -> List[str]:
        """从 manifest 返回全部模块 ID，不物化实现。"""
        return [spec.id for spec in self._specs]

    def get_capability_index(self) -> Dict[str, List[str]]:
        """取能力方法名到提供者模块标识的倒排索引。

        用于诊断系统里有哪些能力、分别由哪些模块提供。单个模块推导能力出错时记
        debug 日志后跳过该模块，不中断整张表。

        :return: {能力方法名: [模块标识, ...]}, 键与值均排序
        """
        with self._lock:
            running = dict(self._running_modules)
        index: Dict[str, List[str]] = {}
        for module_id, module in running.items():
            try:
                extension = HostModuleExtension(module)
                capabilities = extension.capability_names()
            except Exception as err:
                logger.debug("推导模块 %s 能力出错：%s", module_id, str(err))
                continue
            for capability in capabilities:
                index.setdefault(capability, []).append(module_id)
        return {name: sorted(owners) for name, owners in sorted(index.items())}

    def get_module_capabilities(self, module_id: str) -> List[str]:
        """取一个运行态模块提供的能力方法名列表。

        判定复用 `HostModuleExtension.capability_names()` 的实现，与 `providers_for`
        同一份定义，不重新判定「什么算能力」。

        :param module_id: 模块标识
        :return: 能力方法名列表，排序后；模块未运行时为空列表
        """
        with self._lock:
            module = self._running_modules.get(module_id)
        if module is None:
            return []
        try:
            extension = HostModuleExtension(module)
            return sorted(extension.capability_names())
        except Exception as err:
            logger.debug("推导模块 %s 能力出错：%s", module_id, str(err))
            return []

    def list_specs(self) -> tuple[CapabilitySpec, ...]:
        """返回全部轻量模块声明，包含物化或启动失败的能力。"""
        return self._specs

    def get_specs(self) -> tuple[CapabilitySpec, ...]:
        """兼容内部调用命名，返回与 `list_specs` 相同的声明快照。"""
        return self.list_specs()
