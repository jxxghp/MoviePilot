"""Managed Resource 的生命周期入口。"""

from app.runtime.capabilities.runtime import CapabilityRuntime
from app.startup.composition.resource import (
    configure_managed_resource_composition,
    reset_managed_resource_composition,
    stop_managed_resource_composition,
)


def init_managed_resources() -> CapabilityRuntime:
    """委托组合根构建并发布托管资源 Runtime。"""
    return configure_managed_resource_composition()


async def stop_managed_resources() -> bool:
    """委托组合根关闭托管资源 Runtime。"""
    return await stop_managed_resource_composition()


def reset_managed_resources() -> None:
    """委托组合根释放已关闭的托管资源 Runtime。"""
    reset_managed_resource_composition()
