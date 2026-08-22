"""Managed Resource 的声明发现与 Capability Runtime 适配器。"""

from __future__ import annotations

import asyncio
import importlib
import inspect
from pathlib import Path
from typing import Any, Iterable

from app.runtime.capabilities.errors import CapabilityAdapterContractError
from app.runtime.capabilities.model import (
    ActivationPolicy,
    AdapterExecutionMode,
    CapabilitySpec,
)
from app.runtime.capabilities.registry import CapabilityRegistry
from app.runtime.managed_resources import (
    MANAGED_RESOURCE_ASYNC_KIND,
    MANAGED_RESOURCE_SYNC_KIND,
)


_DEFAULT_RESOURCE_ROOT = Path(__file__).resolve().parents[3] / "adapters"
_RESOURCE_KINDS = {MANAGED_RESOURCE_SYNC_KIND, MANAGED_RESOURCE_ASYNC_KIND}


def _load_entrypoint(spec: CapabilitySpec) -> Any:
    """解析声明中的 canonical 实现对象，不创建资源实例。"""
    module_name, symbol_name = spec.entrypoint.split(":", maxsplit=1)
    module = importlib.import_module(module_name)
    try:
        return getattr(module, symbol_name)
    except AttributeError as error:
        raise CapabilityAdapterContractError(
            f"{spec.entrypoint} 未公开 Managed Resource 实现"
        ) from error


def _create_candidate(spec: CapabilitySpec, implementation: Any) -> Any:
    """通过零参数工厂创建资源候选，实例在 start 成功前不可见。"""
    if not callable(implementation):
        raise CapabilityAdapterContractError(
            f"{spec.entrypoint} 不是可调用的 Managed Resource 工厂"
        )
    candidate = implementation()
    if candidate is None or inspect.isawaitable(candidate):
        close = getattr(candidate, "close", None)
        if callable(close):
            close()
        raise CapabilityAdapterContractError(
            f"{spec.entrypoint} 必须同步返回资源候选"
        )
    return candidate


def _resource_method(spec: CapabilitySpec, candidate: Any, name: str) -> Any:
    """读取必需生命周期方法并生成稳定合同错误。"""
    callback = getattr(candidate, name, None)
    if not callable(callback):
        raise CapabilityAdapterContractError(
            f"{spec.entrypoint} 的资源候选缺少 {name}()"
        )
    return callback


class SyncManagedResourceAdapter:
    """把同步 start/stop 资源接入 Capability Runtime。"""

    execution_mode = AdapterExecutionMode.SYNC

    @staticmethod
    def materialize(spec: CapabilitySpec) -> Any:
        """解析资源工厂。"""
        return _load_entrypoint(spec)

    @staticmethod
    def create(
        spec: CapabilitySpec,
        implementation: Any,
        _generation: int,
        _previous: Any = None,
    ) -> Any:
        """创建尚未发布的同步资源候选。"""
        return _create_candidate(spec, implementation)

    @staticmethod
    def start(spec: CapabilitySpec, candidate: Any, _generation: int) -> None:
        """启动同步候选；同步 kind 不接受 awaitable 返回值。"""
        result = _resource_method(spec, candidate, "start")()
        if inspect.isawaitable(result):
            close = getattr(result, "close", None)
            if callable(close):
                close()
            raise CapabilityAdapterContractError(
                f"{spec.entrypoint}.start() 返回 awaitable，与同步 kind 不匹配"
            )

    @staticmethod
    def stop(spec: CapabilitySpec, instance: Any, _generation: int) -> None:
        """停止同步资源，异常交由 Runtime 保留资源所有权并支持重试。"""
        result = _resource_method(spec, instance, "stop")()
        if inspect.isawaitable(result):
            close = getattr(result, "close", None)
            if callable(close):
                close()
            raise CapabilityAdapterContractError(
                f"{spec.entrypoint}.stop() 返回 awaitable，与同步 kind 不匹配"
            )

    @staticmethod
    def cleanup(
        spec: CapabilitySpec,
        candidate: Any,
        generation: int,
        _error: BaseException,
    ) -> None:
        """启动失败时按同一 stop 合同清理尚未发布的候选。"""
        SyncManagedResourceAdapter.stop(spec, candidate, generation)


class AsyncManagedResourceAdapter:
    """把异步 start/stop 资源接入 Capability Runtime。"""

    execution_mode = AdapterExecutionMode.ASYNC

    @staticmethod
    async def materialize(spec: CapabilitySpec) -> Any:
        """在线程中解析资源工厂，避免第三方导入阻塞事件循环。"""
        return await asyncio.to_thread(_load_entrypoint, spec)

    @staticmethod
    async def create(
        spec: CapabilitySpec,
        implementation: Any,
        _generation: int,
        _previous: Any = None,
    ) -> Any:
        """创建尚未发布的异步资源候选。"""
        return _create_candidate(spec, implementation)

    @staticmethod
    async def start(spec: CapabilitySpec, candidate: Any, _generation: int) -> None:
        """等待异步候选完成启动。"""
        result = _resource_method(spec, candidate, "start")()
        if not inspect.isawaitable(result):
            raise CapabilityAdapterContractError(
                f"{spec.entrypoint}.start() 必须返回 awaitable"
            )
        await result

    @staticmethod
    async def stop(spec: CapabilitySpec, instance: Any, _generation: int) -> None:
        """等待异步资源完成停止。"""
        result = _resource_method(spec, instance, "stop")()
        if not inspect.isawaitable(result):
            raise CapabilityAdapterContractError(
                f"{spec.entrypoint}.stop() 必须返回 awaitable"
            )
        await result

    @staticmethod
    async def cleanup(
        spec: CapabilitySpec,
        candidate: Any,
        generation: int,
        _error: BaseException,
    ) -> None:
        """启动失败时等待同一 stop 合同清理候选。"""
        await AsyncManagedResourceAdapter.stop(spec, candidate, generation)


def _validate_registry(registry: CapabilityRegistry) -> None:
    """固定类别级声明合同，资源只能由显式首用触发。"""
    for spec in registry.list_specs():
        if set(spec.metadata) != {"name"}:
            raise ValueError(f"{spec.source}: Managed Resource metadata 只能包含 name")
        if spec.activation is not ActivationPolicy.ON_FIRST_USE:
            raise ValueError(f"{spec.source}: Managed Resource 必须使用 on_first_use")
        if spec.selector is not None or spec.watch:
            raise ValueError(f"{spec.source}: Managed Resource 不接受配置 selector 或 watch")


def build_managed_resource_registry(
    roots: Iterable[Path | str] | None = None,
) -> CapabilityRegistry:
    """从 data-only manifest 构建不导入资源实现的注册表。"""
    registry = CapabilityRegistry.discover(
        tuple(roots) if roots is not None else (_DEFAULT_RESOURCE_ROOT,),
        kinds=_RESOURCE_KINDS,
        selector_schemas={},
    )
    _validate_registry(registry)
    return registry
