"""Managed Resource 的启动组合与进程关闭入口。"""

from __future__ import annotations

import threading
from typing import Optional

from app.runtime.capabilities.runtime import CapabilityRuntime
from app.runtime.extensions.resource import (
    AsyncManagedResourceAdapter,
    SyncManagedResourceAdapter,
    build_managed_resource_registry,
)
from app.runtime.resources import (
    MANAGED_RESOURCE_ASYNC_KIND,
    MANAGED_RESOURCE_SYNC_KIND,
    configure_managed_resource_runtime,
    reset_managed_resource_runtime,
)

_runtime_lock = threading.RLock()
_managed_resource_runtime: Optional[CapabilityRuntime] = None


def init_managed_resources() -> CapabilityRuntime:
    """构建并注入资源 Runtime；只发现声明，不物化或启动任何资源。"""
    global _managed_resource_runtime
    with _runtime_lock:
        if _managed_resource_runtime is None:
            _managed_resource_runtime = CapabilityRuntime(
                build_managed_resource_registry(),
                adapters={
                    MANAGED_RESOURCE_SYNC_KIND: SyncManagedResourceAdapter(),
                    MANAGED_RESOURCE_ASYNC_KIND: AsyncManagedResourceAdapter(),
                },
            )
        configure_managed_resource_runtime(_managed_resource_runtime)
        return _managed_resource_runtime


async def stop_managed_resources() -> bool:
    """关闭已初始化的资源 Runtime，并返回 owner 是否收敛。"""
    with _runtime_lock:
        runtime = _managed_resource_runtime
    if runtime is None:
        return True
    return await runtime.shutdown_async(reason="application_shutdown")


def reset_managed_resources() -> None:
    """释放已关闭的具体 Runtime 及其门面引用，不模拟进程内完整重启。"""
    global _managed_resource_runtime
    with _runtime_lock:
        runtime = _managed_resource_runtime
        if runtime is not None and not runtime.is_shutdown:
            raise RuntimeError("Managed Resource Runtime 尚未关闭，不能释放 owner")
        reset_managed_resource_runtime()
        _managed_resource_runtime = None
