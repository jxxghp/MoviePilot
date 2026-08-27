"""虚拟显示适配器及旧 DisplayHelper 兼容入口。"""

from __future__ import annotations

from importlib import import_module
from typing import Any

from app.foundation.singleton import Singleton
from app.runtime.log import logger
from app.runtime.resources import (
    acquire_managed_resource,
    stop_managed_resource,
)

DISPLAY_CAPABILITY_ID = "host.display"


class DisplayHelper(metaclass=Singleton):
    """保留旧构造 API，并把资源所有权委托给 host.display 能力。"""

    def __init__(self) -> None:
        """显式构造旧门面时激活虚拟显示，失败保持旧 API 的日志语义。"""
        try:
            acquire_managed_resource(
                DISPLAY_CAPABILITY_ID,
                reason="legacy_display_helper",
                retry=True,
            )
        except Exception as error:
            logger.error("DisplayHelper init error: %s", error)

    def stop(self) -> None:
        """停止已激活的虚拟显示；未配置 Runtime 时保持幂等。"""
        stop_managed_resource(
            DISPLAY_CAPABILITY_ID,
            reason="legacy_display_helper_stop",
        )


__all__ = ["DISPLAY_CAPABILITY_ID", "DisplayHelper", "VirtualDisplayResource"]


def __getattr__(name: str) -> Any:
    """按需公开资源实现，普通兼容导入不加载显示后端。"""
    if name != "VirtualDisplayResource":
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(
        import_module("app.adapters.system.display.resource"),
        "VirtualDisplayResource",
    )
    globals()[name] = value
    return value
