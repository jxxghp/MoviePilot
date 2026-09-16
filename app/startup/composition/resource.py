"""托管资源 Runtime 与站点资源安装的宿主组合根。"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Optional

from app.adapters.system.resource import (
    configure_resource_version_provider,
    reset_resource_version_provider,
)
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


def configure_managed_resource_composition() -> CapabilityRuntime:
    """构建并注入资源 Runtime；只发现声明，不提前启动具体资源。"""
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


async def stop_managed_resource_composition() -> bool:
    """关闭已初始化的资源 Runtime，并返回 owner 是否收敛。"""
    with _runtime_lock:
        runtime = _managed_resource_runtime
    if runtime is None:
        return True
    return await runtime.shutdown_async(reason="application_shutdown")


def reset_managed_resource_composition() -> None:
    """释放已关闭的具体 Runtime 及其门面引用。"""
    global _managed_resource_runtime
    with _runtime_lock:
        runtime = _managed_resource_runtime
        if runtime is not None and not runtime.is_shutdown:
            raise RuntimeError("Managed Resource Runtime 尚未关闭，不能释放 owner")
        reset_managed_resource_runtime()
        _managed_resource_runtime = None


def configure_site_resource_versions(
    version_provider: Callable[[], tuple[str, str]],
) -> None:
    """装配运行期资源版本读取器，不在应用启动后下载或替换资源。"""
    configure_resource_version_provider(version_provider)


def install_site_resources(
    version_provider: Callable[[], tuple[str, str]],
) -> bool:
    """兼容旧调用方，仅装配版本读取器并禁止启动阶段直接安装资源。"""
    configure_site_resource_versions(version_provider)
    return False


def reset_site_resource_composition() -> None:
    """撤销当前 lifespan 的资源版本读取器。"""
    reset_resource_version_provider()
