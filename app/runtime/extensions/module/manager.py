"""
宿主模块目录与生命周期门面，协调配置化模块启动及能力分发。
"""

from __future__ import annotations

import sys
import threading
from typing import Any, Generator, List, Optional, Tuple, Union

from app.foundation.reflection import ObjectUtils
from app.foundation.singleton import Singleton
from app.runtime.capabilities.model import (
    ActivationPolicy,
    CapabilityLifecycleState,
    CapabilityObservation,
    CapabilitySpec,
)
from app.runtime.capabilities.runtime import CapabilityRuntime
from app.runtime.event.binding import EventHandlerBinding
from app.runtime.events import Event, eventmanager
from app.runtime.extensions.module.adapter import (
    HOST_MODULE_KIND,
    MODULE_ENABLE_SETTING,
    NON_SWITCHABLE_HOST_MODULE_IDS,
    HostModuleAdapter,
    build_host_module_registry,
    capture_host_module_config,
    should_run_host_module,
)
from app.runtime.log import logger
from app.runtime.settings import get_runtime_setting
from app.schemas.types import (
    DownloaderType,
    EventType,
    MediaRecognizeType,
    MediaServerType,
    ModuleType,
    NotificationChannel,
    OtherModulesType,
    StorageSchema,
)


class ModuleManager(metaclass=Singleton):
    """
    以 Capability Runtime 管理宿主模块，并保留旧插件同步查询合同。
    """

    SubType = Union[
        DownloaderType,
        MediaServerType,
        NotificationChannel,
        StorageSchema,
        OtherModulesType,
        MediaRecognizeType,
    ]

    def __init__(self) -> None:
        """
        发现 data-only manifest，并按当前配置激活所需宿主模块。
        """
        self._lock = threading.RLock()
        self._lifecycle_lock = threading.RLock()
        self._modules: dict[str, type] = {}
        self._running_modules: dict[str, Any] = {}
        registry = build_host_module_registry()
        self._runtime = CapabilityRuntime(
            registry,
            adapters={HOST_MODULE_KIND: HostModuleAdapter()},
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
    def _observe_transition(observation: CapabilityObservation) -> None:
        """
        把 Runtime 的稳定转换结果接入现有日志面。
        """
        if observation.outcome == "failed":
            logger.error(
                "Host Module %s %s 失败：%s",
                observation.capability_id,
                observation.operation,
                observation.error,
            )
        elif observation.outcome == "succeeded":
            log_fn = logger.info if observation.operation == "reload" else logger.debug
            log_fn(
                "Host Module %s %s 完成，generation=%s，耗时=%.2fms",
                observation.capability_id,
                observation.operation,
                observation.generation,
                observation.duration_ms,
            )

    @staticmethod
    def _event_changed_keys(event: Optional[Event]) -> set[str]:
        """
        兼容对象和 dict 两种配置事件载荷。
        """
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
        """
        更新旧 `_modules` 视图，但不改变能力资源生命周期。
        """
        with self._lock:
            self._modules[module_id] = implementation
        return implementation

    def _consumer_materialized_class(self, spec: CapabilitySpec) -> Optional[type]:
        """
        识别插件显式旧导入产生的真实类，不触发新的 Python import。
        """
        module_name, symbol_name = spec.entrypoint.split(":", maxsplit=1)
        module = sys.modules.get(module_name)
        namespace = getattr(module, "__dict__", None) if module is not None else None
        if not isinstance(namespace, dict):
            return None
        implementation = namespace.get(symbol_name)
        return implementation if isinstance(implementation, type) else None

    def _refresh_running_projection(self) -> None:
        """
        从 Runtime 已发布实例重建插件可见的运行模块字典。
        """
        running = {
            spec.id: instance
            for spec in self._specs
            if (instance := self._runtime.get_running(spec.id)) is not None
        }
        with self._lock:
            self._running_modules = running

    def resolve_event_handler_instance(
        self,
        owner_class: type,
    ) -> Optional[EventHandlerBinding]:
        """
        按类型身份绑定管理器自身或运行模块，停止态阻断 fallback 构造。
        """
        if owner_class is type(self):
            return EventHandlerBinding(
                instance=self,
                owner_name=type(self).__name__,
            )
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
            return EventHandlerBinding(
                instance=self._runtime.get_running(spec.id),
                owner_name=str(spec.metadata["name"]),
            )
        return None

    def _select_reconcile_specs(
        self,
        changed_keys: Optional[set[str]] = None,
    ) -> tuple[CapabilitySpec, ...]:
        """
        按变更键筛选本轮需要协调的宿主模块声明。
        """
        return tuple(
            spec
            for spec in self._specs
            if (
                changed_keys is None
                or changed_keys.intersection(spec.watch)
                or (
                    spec.activation is ActivationPolicy.BOOTSTRAP
                    and MODULE_ENABLE_SETTING in changed_keys
                )
            )
        )

    @staticmethod
    def _module_init_errors(instance: object) -> dict[Optional[str], str]:
        """
        读取服务模块已记录的实例初始化错误；未实现该接口的模块返回空字典。
        """
        getter = getattr(instance, "get_init_errors", None)
        if not callable(getter):
            return {}
        errors = getter()
        return dict(errors) if isinstance(errors, dict) else {}

    def _reconcile(
        self,
        *,
        reason: str,
        changed_keys: Optional[set[str]] = None,
        reload_running: bool = False,
        reload_degraded_only: bool = False,
    ) -> bool:
        """
        以一次配置快照串行协调需要启动、重载或停止的能力。

        :param reason: 触发协调的原因标识
        :param changed_keys: 变更的配置键集合；为 None 时协调全部模块
        :param reload_running: 是否对仍需运行的已运行模块执行热重载
        :param reload_degraded_only: 为 True 时仅重试未运行或存在部分实例初始化失败的模块
        :return: 本轮是否对任意模块发起了激活、重载或停止操作
        """
        acted = False
        with self._lifecycle_lock:
            selected = self._select_reconcile_specs(changed_keys)
            snapshot = capture_host_module_config(selected)
            for spec in selected:
                desired = should_run_host_module(spec, snapshot)
                running = self._runtime.get_running(spec.id)
                should_reload = (
                    reload_running
                    or (reload_degraded_only and running is not None and bool(self._module_init_errors(running)))
                )
                try:
                    if desired and running is None:
                        acted = True
                        instance = self._runtime.activate(
                            spec.id,
                            reason=reason,
                            retry=True,
                        )
                        self._remember_materialized(spec.id, type(instance))
                    elif desired and running is not None and should_reload:
                        acted = True
                        instance = self._runtime.reload(spec.id, reason=reason)
                        self._remember_materialized(spec.id, type(instance))
                    elif not desired and running is not None:
                        acted = True
                        self._runtime.stop(spec.id, reason=reason)
                except Exception:
                    # 单能力失败由 Runtime 完整记录；其它无依赖能力继续 reconcile。
                    continue
            if acted or not reload_degraded_only:
                self._refresh_running_projection()
        return acted

    def load_modules(self) -> None:
        """
        按当前配置启动未运行模块；已运行模块保持当前 generation。
        """
        self._reconcile(reason="module_manager_load")

    def handle_config_changed(self, event: Event) -> None:
        """
        配置变更时仅协调 watch 命中的能力，并保证单一生命周期 writer。
        """
        changed_keys = self._event_changed_keys(event)
        if not changed_keys:
            return
        event_data = event.event_data
        change_type = (
            event_data.get("change_type", "update")
            if isinstance(event_data, dict)
            else getattr(event_data, "change_type", "update")
        )
        if change_type == "recover":
            self.reconcile_unhealthy_keys(changed_keys)
            return
        self._reconcile(
            reason="config_changed",
            changed_keys=changed_keys,
            reload_running=True,
        )

    def reconcile_unhealthy_keys(self, changed_keys: set[str]) -> bool:
        """
        在用户重新保存未变化的配置时，仅对异常或未就绪的关联模块执行恢复协调。

        健康运行中的模块不会被重载或重建连接。

        :param changed_keys: 本次保存涉及的配置键集合
        :return: 是否尝试协调了至少一个异常或状态不一致的模块
        """
        if not changed_keys:
            return False
        return self._reconcile(
            reason="config_recovery",
            changed_keys=changed_keys,
            reload_running=False,
            reload_degraded_only=True,
        )

    def stop(self) -> None:
        """
        停止全部运行模块但保留 Runtime，使旧插件可随后再次 load。
        """
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

    def shutdown(self) -> bool:
        """
        不可逆停止 Runtime，并返回所有模块 owner 是否收敛。
        """
        logger.info("正在关闭模块运行时...")
        with self._lifecycle_lock:
            converged = self._runtime.shutdown(reason="application_shutdown")
            self._refresh_running_projection()
        if converged:
            eventmanager.remove_event_listener(
                EventType.ConfigChanged,
                self.handle_config_changed,
            )
            eventmanager.unregister_handler_instance_resolver("modules")
            logger.info("模块运行时关闭完成")
        else:
            logger.error("模块运行时关闭后仍有资源 owner 未收敛")
        return converged

    def reload(self) -> None:
        """
        保留旧插件可观察的 stop、load、ModuleReload 同步顺序。
        """
        with self._lifecycle_lock:
            self.stop()
            self.load_modules()
            eventmanager.send_event(etype=EventType.ModuleReload, data={})

    def test(self, modleid: str) -> Tuple[bool, str]:
        """
        测试已运行模块；初始化或重载失败的模块返回具体错误，未启用模块保持旧合同返回 `(False, "")`。
        """
        module = self.get_running_module(modleid)
        if module is None:
            if modleid and self._runtime.get_spec(modleid) is not None:
                snapshot = self._runtime.snapshot(modleid)
                if snapshot.lifecycle is CapabilityLifecycleState.FAILED:
                    detail = f"：{snapshot.error}" if snapshot.error else ""
                    return False, f"模块初始化或重载失败{detail}"
            return False, ""
        if hasattr(module, "test") and ObjectUtils.check_method(module.test):
            result = module.test()
            return result if result else (False, "")
        return True, "模块不支持测试"

    def get_unhealthy_module_issues(
        self,
        module_type: Optional[ModuleType] = None,
    ) -> list[dict[str, Any]]:
        """
        返回当前配置要求启用但未正常运行或存在实例初始化失败的宿主模块诊断项。

        仅读取本地生命周期快照与模块已记录的初始化错误，不触发模块物化、重载或网络探测。

        :param module_type: 可选的模块类型过滤；为空时检查全部应启用模块
        :return: 异常模块诊断字典列表，包含模块 ID、名称、类型、状态、错误说明和实例错误映射
        """
        issues: list[dict[str, Any]] = []
        for spec in self.list_enabled_specs():
            if module_type is not None and spec.metadata.get("type") != module_type.value:
                continue
            running = self._runtime.get_running(spec.id)
            if running is None:
                snapshot = self._runtime.snapshot(spec.id)
                issues.append(
                    {
                        "id": spec.id,
                        "name": str(spec.metadata.get("name") or spec.id),
                        "type": str(spec.metadata.get("type") or ""),
                        "lifecycle": snapshot.lifecycle.value,
                        "error": snapshot.error,
                        "init_errors": {},
                    }
                )
                continue
            init_errors = self._module_init_errors(running)
            if init_errors:
                issues.append(
                    {
                        "id": spec.id,
                        "name": str(spec.metadata.get("name") or spec.id),
                        "type": str(spec.metadata.get("type") or ""),
                        "lifecycle": "degraded",
                        "error": "；".join(
                            f"{name or '未命名'}：{err}"
                            for name, err in init_errors.items()
                        ),
                        "init_errors": init_errors,
                    }
                )
        return issues

    @staticmethod
    def check_setting(setting: Optional[tuple[str, object]]) -> bool:
        """
        保留旧模块开关的 truthy 与 membership 判定语义。
        """
        if not setting:
            return True
        switch, value = setting
        option = get_runtime_setting(switch)
        if not option:
            return False
        if value is True:
            return True
        return value in option

    def get_running_module(self, module_id: str) -> Any:
        """
        根据模块 ID 返回已发布的运行实例，不触发物化。
        """
        if not module_id or self._runtime.get_spec(module_id) is None:
            return None
        return self._runtime.get_running(module_id)

    def _running_snapshot(self) -> tuple[Any, ...]:
        """
        直接读取 Runtime 发布视图，转换期间不暴露旧或候选实例。
        """
        return tuple(
            instance
            for spec in self._specs
            if (instance := self._runtime.get_running(spec.id)) is not None
        )

    def get_running_modules(self, method: str) -> Generator[object, None, None]:
        """
        返回实现了指定方法的运行模块快照。
        """
        for module in self._running_snapshot():
            candidate = getattr(module, method, None)
            if callable(candidate) and ObjectUtils.check_method(candidate):
                yield module

    def get_running_type_modules(self, module_type: ModuleType) -> Generator[object, None, None]:
        """
        返回指定类型的运行模块快照。
        """
        for module in self._running_snapshot():
            if module.get_type() == module_type:
                yield module

    def get_running_subtype_module(self, module_subtype: SubType) -> Generator[object, None, None]:
        """
        返回指定子类型的运行模块快照。
        """
        for module in self._running_snapshot():
            if module.get_subtype() == module_subtype:
                yield module

    def get_module(self, module_id: str) -> Any:
        """
        显式物化并返回 canonical 模块类；失败保持旧合同返回 None。
        """
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
        """
        兼容性显式物化全部真实类；单个失败不阻断其它模块。
        """
        for spec in self._specs:
            self.get_module(spec.id)
        with self._lock:
            return dict(self._modules)

    def get_module_ids(self) -> List[str]:
        """
        从 manifest 返回全部模块 ID，不物化实现。
        """
        return [spec.id for spec in self._specs]

    def list_specs(self) -> tuple[CapabilitySpec, ...]:
        """
        返回全部轻量模块声明，包含物化或启动失败的能力。
        """
        return self._specs

    def list_switchable_specs(self) -> tuple[CapabilitySpec, ...]:
        """
        返回可由用户统一开关的内置媒体模块声明，排除核心模块及已有独立开关的模块。
        """
        return tuple(
            spec
            for spec in self._specs
            if spec.activation is ActivationPolicy.BOOTSTRAP and spec.id not in NON_SWITCHABLE_HOST_MODULE_IDS
        )

    def list_enabled_specs(self) -> tuple[CapabilitySpec, ...]:
        """
        返回当前配置下应对外展示的模块声明。

        该视图按 selector 重新计算“应启用”状态，因此既不会展示未配置的服务模块，
        也不会因为模块启动失败而把应启用的模块从健康检查中隐藏。
        """
        snapshot = capture_host_module_config(self._specs)
        return tuple(
            spec
            for spec in self._specs
            if should_run_host_module(spec, snapshot)
        )

    def get_specs(self) -> tuple[CapabilitySpec, ...]:
        """
        兼容内部调用命名，返回与 `list_specs` 相同的声明快照。
        """
        return self.list_specs()
