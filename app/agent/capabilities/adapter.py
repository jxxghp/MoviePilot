"""Agent canonical entrypoint 的 Capability Runtime 适配器。"""

from __future__ import annotations

import asyncio
import importlib
import inspect
from pathlib import Path
from typing import Any, Iterable, Mapping

from app.agent.capabilities import AGENT_ENTRYPOINT_KIND, AGENT_SERVICE_KIND
from app.runtime.capabilities.errors import CapabilityAdapterContractError
from app.runtime.capabilities.model import (
    ActivationPolicy,
    AdapterExecutionMode,
    CapabilitySpec,
    SelectorSchema,
)
from app.runtime.capabilities.registry import CapabilityRegistry
from app.runtime.settings import get_runtime_setting, has_runtime_setting

_DEFAULT_CAPABILITY_ROOT = Path(__file__).resolve().parent
_SETTING_SELECTOR = "setting_truthy"


def _validate_setting_selector(config: Mapping[str, Any]) -> None:
    """限制 selector 只能读取已声明的应用设置。"""
    key = config["key"]
    if not isinstance(key, str) or not key or not has_runtime_setting(key):
        raise ValueError(f"未知应用设置：{key!r}")


AGENT_SELECTOR_SCHEMAS = {
    _SETTING_SELECTOR: SelectorSchema(
        required_fields=frozenset({"key"}),
        validator=_validate_setting_selector,
    )
}


def _load_entrypoint(spec: CapabilitySpec) -> Any:
    """按 manifest 解析 canonical 符号，不创建额外业务对象。"""
    module_name, symbol_name = spec.entrypoint.split(":", maxsplit=1)
    module = importlib.import_module(module_name)
    try:
        return getattr(module, symbol_name)
    except AttributeError as error:
        raise CapabilityAdapterContractError(
            f"{spec.entrypoint} 未公开 Agent entrypoint"
        ) from error


def _lifecycle_method(spec: CapabilitySpec, candidate: Any, name: str) -> Any:
    """读取 Agent Service 必需的异步生命周期方法。"""
    callback = getattr(candidate, name, None)
    if not callable(callback):
        raise CapabilityAdapterContractError(
            f"{spec.entrypoint} 的 Agent Service 缺少 {name}()"
        )
    return callback


class AgentEntrypointAdapter:
    """把 canonical Python 符号作为无资源副作用的同步能力发布。"""

    execution_mode = AdapterExecutionMode.SYNC

    @staticmethod
    def materialize(spec: CapabilitySpec) -> Any:
        """按 manifest entrypoint 导入 canonical 符号。"""
        return _load_entrypoint(spec)

    @staticmethod
    def create(
        _spec: CapabilitySpec,
        implementation: Any,
        _generation: int,
        _previous: Any = None,
    ) -> Any:
        """发布 canonical 符号本身，不创建第二份业务对象。"""
        return implementation

    @staticmethod
    def start(
        _spec: CapabilitySpec,
        _candidate: Any,
        _generation: int,
    ) -> None:
        """entrypoint 不拥有业务资源，初始化由独立 service 能力负责。"""

    @staticmethod
    def stop(
        _spec: CapabilitySpec,
        _instance: Any,
        _generation: int,
    ) -> None:
        """撤销入口可见性；业务资源由独立 service 能力关闭。"""

    @staticmethod
    def cleanup(
        _spec: CapabilitySpec,
        _candidate: Any,
        _generation: int,
        _error: BaseException,
    ) -> None:
        """entrypoint 启动无副作用，因此失败候选无需额外释放。"""


class AgentServiceAdapter:
    """把具备 initialize/close 的 canonical 对象接入异步资源生命周期。"""

    execution_mode = AdapterExecutionMode.ASYNC

    @staticmethod
    async def materialize(spec: CapabilitySpec) -> Any:
        """在线程中导入 canonical service，避免阻塞应用事件循环。"""
        return await asyncio.to_thread(_load_entrypoint, spec)

    @staticmethod
    async def create(
        _spec: CapabilitySpec,
        implementation: Any,
        _generation: int,
        _previous: Any = None,
    ) -> Any:
        """复用 canonical service，不复制其内部队列和后台任务所有权。"""
        return implementation

    @staticmethod
    async def start(
        spec: CapabilitySpec,
        candidate: Any,
        _generation: int,
    ) -> None:
        """等待 service 在当前应用事件循环完成初始化。"""
        result = _lifecycle_method(spec, candidate, "initialize")()
        if not inspect.isawaitable(result):
            raise CapabilityAdapterContractError(
                f"{spec.entrypoint}.initialize() 必须返回 awaitable"
            )
        await result

    @staticmethod
    async def stop(
        spec: CapabilitySpec,
        instance: Any,
        _generation: int,
    ) -> None:
        """等待 service 停止后台任务并释放其资源。"""
        result = _lifecycle_method(spec, instance, "close")()
        if not inspect.isawaitable(result):
            raise CapabilityAdapterContractError(
                f"{spec.entrypoint}.close() 必须返回 awaitable"
            )
        converged = await result
        if converged is False:
            raise CapabilityAdapterContractError(
                f"{spec.entrypoint}.close() 返回未收敛，保留 service owner"
            )

    @staticmethod
    async def cleanup(
        spec: CapabilitySpec,
        candidate: Any,
        generation: int,
        _error: BaseException,
    ) -> None:
        """初始化失败或关闭竞态时按相同 close 合同释放部分资源。"""
        await AgentServiceAdapter.stop(spec, candidate, generation)


def _validate_registry(registry: CapabilityRegistry) -> None:
    """固定 entrypoint 物化轴与 service 资源轴的声明合同。"""
    for spec in registry.list_specs():
        if set(spec.metadata) != {"name"}:
            raise ValueError(f"{spec.source}: Agent Capability metadata 只能包含 name")
        if spec.kind == AGENT_ENTRYPOINT_KIND:
            if spec.activation is not ActivationPolicy.ON_FIRST_USE:
                raise ValueError(
                    f"{spec.source}: Agent entrypoint 必须使用 on_first_use"
                )
            if spec.selector is not None or spec.watch:
                raise ValueError(
                    f"{spec.source}: Agent entrypoint 不接受 selector 或 watch"
                )
            continue
        if spec.activation is not ActivationPolicy.WHEN_CONFIGURED:
            raise ValueError(f"{spec.source}: Agent Service 必须使用 when_configured")
        selector = spec.selector
        if selector is None or selector.kind != _SETTING_SELECTOR:
            raise ValueError(f"{spec.source}: Agent Service 必须声明 setting_truthy")
        selector_key = str(selector.config["key"])
        if spec.watch != (selector_key,):
            raise ValueError(
                f"{spec.source}: Agent Service watch 必须只包含 selector key"
            )


def build_agent_capability_registry(
    roots: Iterable[Path | str] | None = None,
) -> CapabilityRegistry:
    """发现 data-only Agent manifests，不导入编排器、Provider 或工具实现。"""
    registry = CapabilityRegistry.discover(
        tuple(roots) if roots is not None else (_DEFAULT_CAPABILITY_ROOT,),
        kinds={AGENT_ENTRYPOINT_KIND, AGENT_SERVICE_KIND},
        selector_schemas=AGENT_SELECTOR_SCHEMAS,
    )
    _validate_registry(registry)
    return registry


def should_run_agent_service(spec: CapabilitySpec) -> bool:
    """依据 manifest selector 判断 service 是否应拥有运行实例。"""
    selector = spec.selector
    if (
        spec.kind != AGENT_SERVICE_KIND
        or selector is None
        or selector.kind != _SETTING_SELECTOR
    ):
        raise ValueError(f"{spec.source}: 不是可协调的 Agent Service 声明")
    return bool(get_runtime_setting(selector.config["key"]))
