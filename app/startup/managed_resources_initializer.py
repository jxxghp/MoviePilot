"""Managed Resource 的启动组合与进程关闭入口。"""

from __future__ import annotations

import threading
from typing import Optional

from app.runtime.capabilities.runtime import CapabilityRuntime
from app.runtime.extensions.lifecycle.managed_resource_adapter import (
    AsyncManagedResourceAdapter,
    SyncManagedResourceAdapter,
    build_managed_resource_registry,
)
from app.runtime.managed_resources import (
    MANAGED_RESOURCE_ASYNC_KIND,
    MANAGED_RESOURCE_SYNC_KIND,
    configure_managed_resource_runtime,
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


async def stop_managed_resources() -> None:
    """关闭已经初始化的资源 Runtime；未初始化时不执行发现或激活。"""
    with _runtime_lock:
        runtime = _managed_resource_runtime
    if runtime is None:
        return
    await runtime.shutdown_async(reason="application_shutdown")
