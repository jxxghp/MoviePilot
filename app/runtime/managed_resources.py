"""进程级托管资源的轻量调用门面。"""

from __future__ import annotations

import asyncio
import threading
from typing import Any, Optional, Protocol


MANAGED_RESOURCE_SYNC_KIND = "managed_resource.sync"
MANAGED_RESOURCE_ASYNC_KIND = "managed_resource.async"


class ManagedResourceRuntime(Protocol):
    """Managed Resource 门面依赖的最小 Capability Runtime 合同。"""

    @property
    def is_shutdown(self) -> bool:
        """返回 Runtime 是否已进入不可逆关闭态。"""

    def get_spec(self, capability_id: str) -> Any:
        """返回资源声明。"""

    def get_running(self, capability_id: str) -> Any:
        """只查询已发布实例。"""

    def snapshot(self, capability_id: str) -> Any:
        """返回资源状态快照。"""

    def observations(self, capability_id: Optional[str] = None) -> tuple[Any, ...]:
        """返回资源转换观测。"""

    def activate(self, capability_id: str, *, reason: str, retry: bool = False) -> Any:
        """通过同步 adapter 激活资源。"""

    async def activate_async(
        self,
        capability_id: str,
        *,
        reason: str,
        retry: bool = False,
    ) -> Any:
        """通过异步 adapter 激活资源。"""

    def stop(self, capability_id: str, *, reason: str) -> None:
        """通过同步 adapter 停止资源。"""

    async def stop_async(self, capability_id: str, *, reason: str) -> None:
        """通过异步 adapter 停止资源。"""

    async def shutdown_async(self, *, reason: str) -> bool:
        """关闭混合 Runtime，并返回全部资源是否收敛。"""


_runtime_lock = threading.RLock()
_managed_resource_runtime: Optional[ManagedResourceRuntime] = None


def configure_managed_resource_runtime(runtime: ManagedResourceRuntime) -> None:
    """由启动组合层注入唯一的 Managed Resource Runtime。"""
    if runtime is None:
        raise ValueError("Managed Resource Runtime 不能为空")
    global _managed_resource_runtime
    with _runtime_lock:
        _managed_resource_runtime = runtime


def _runtime(*, required: bool) -> Optional[ManagedResourceRuntime]:
    """读取当前 Runtime；资源使用路径要求启动组合已经完成装配。"""
    with _runtime_lock:
        runtime = _managed_resource_runtime
    if runtime is None and required:
        raise RuntimeError("Managed Resource Runtime 尚未初始化")
    return runtime


def _resource_kind(runtime: ManagedResourceRuntime, capability_id: str) -> str:
    """返回声明的执行模式；未知资源继续沿用 Runtime 的领域错误。"""
    spec = runtime.get_spec(capability_id)
    if spec is None:
        runtime.get_running(capability_id)
        raise RuntimeError(f"未知 Managed Resource：{capability_id}")
    return str(spec.kind)


def acquire_managed_resource(
    capability_id: str,
    *,
    reason: str,
    retry: bool = True,
) -> Any:
    """同步激活一个声明为同步模式的托管资源。"""
    runtime = _runtime(required=True)
    kind = _resource_kind(runtime, capability_id)
    if kind != MANAGED_RESOURCE_SYNC_KIND:
        raise RuntimeError(f"异步 Managed Resource 不能通过同步入口激活：{capability_id}")
    return runtime.activate(capability_id, reason=reason, retry=retry)


async def acquire_managed_resource_async(
    capability_id: str,
    *,
    reason: str,
    retry: bool = True,
) -> Any:
    """异步激活资源；同步资源移交工作线程，避免阻塞事件循环。"""
    runtime = _runtime(required=True)
    kind = _resource_kind(runtime, capability_id)
    if kind == MANAGED_RESOURCE_ASYNC_KIND:
        return await runtime.activate_async(
            capability_id,
            reason=reason,
            retry=retry,
        )
    if kind == MANAGED_RESOURCE_SYNC_KIND:
        return await asyncio.to_thread(
            runtime.activate,
            capability_id,
            reason=reason,
            retry=retry,
        )
    raise RuntimeError(f"未知 Managed Resource kind：{kind}")


def get_running_managed_resource(capability_id: str) -> Any:
    """只查询已发布资源；Runtime 未配置时返回 None，不触发初始化。"""
    runtime = _runtime(required=False)
    if runtime is None:
        return None
    return runtime.get_running(capability_id)


def managed_resource_snapshot(capability_id: str) -> Any:
    """返回资源状态快照；Runtime 未配置时返回 None。"""
    runtime = _runtime(required=False)
    if runtime is None:
        return None
    return runtime.snapshot(capability_id)


def managed_resource_observations(
    capability_id: Optional[str] = None,
) -> tuple[Any, ...]:
    """返回资源转换观测；Runtime 未配置时返回空快照。"""
    runtime = _runtime(required=False)
    if runtime is None:
        return ()
    return runtime.observations(capability_id)


def stop_managed_resource(capability_id: str, *, reason: str) -> None:
    """同步停止资源；Runtime 未配置时保持幂等且不反向初始化。"""
    runtime = _runtime(required=False)
    if runtime is None:
        return
    kind = _resource_kind(runtime, capability_id)
    if kind != MANAGED_RESOURCE_SYNC_KIND:
        raise RuntimeError(f"异步 Managed Resource 不能通过同步入口停止：{capability_id}")
    runtime.stop(capability_id, reason=reason)


async def stop_managed_resource_async(capability_id: str, *, reason: str) -> None:
    """异步停止资源；同步资源移交工作线程。"""
    runtime = _runtime(required=False)
    if runtime is None:
        return
    kind = _resource_kind(runtime, capability_id)
    if kind == MANAGED_RESOURCE_ASYNC_KIND:
        await runtime.stop_async(capability_id, reason=reason)
        return
    if kind == MANAGED_RESOURCE_SYNC_KIND:
        await asyncio.to_thread(runtime.stop, capability_id, reason=reason)
        return
    raise RuntimeError(f"未知 Managed Resource kind：{kind}")


async def shutdown_managed_resource_runtime(*, reason: str) -> bool:
    """关闭已配置 Runtime，未配置时按已收敛处理。"""
    runtime = _runtime(required=False)
    if runtime is None:
        return True
    return await runtime.shutdown_async(reason=reason)
