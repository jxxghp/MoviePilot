"""宿主模块目录的应用层端口。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol


class ModuleRuntime(Protocol):
    """声明入口层消费的模块目录能力。"""

    def __getattr__(self, name: str) -> Any:
        """允许兼容门面访问既有模块管理方法。"""


ModuleRuntimeProvider = Callable[[], ModuleRuntime]


def _unconfigured_runtime() -> ModuleRuntime:
    """拒绝在组合根装配前隐式创建模块管理器。"""
    raise RuntimeError("宿主模块运行时尚未由启动组合根装配")


_runtime_provider: ModuleRuntimeProvider = _unconfigured_runtime


def configure_module_runtime(provider: ModuleRuntimeProvider) -> None:
    """由启动组合根注册模块运行时实例提供器。"""
    global _runtime_provider
    _runtime_provider = provider


def reset_module_runtime() -> None:
    """恢复未装配模块运行时，禁止跨 lifespan 复用旧目录。"""
    global _runtime_provider
    _runtime_provider = _unconfigured_runtime


def get_module_manager() -> ModuleRuntime:
    """返回当前组合根提供的模块目录能力。"""
    return _runtime_provider()


class _ModuleRuntimeProxy(type):
    """把历史 ``ModuleManager`` 调用转发到应用端口。"""

    def __getattr__(cls, name: str) -> Any:
        """转发旧的类级静态调用。"""
        return getattr(get_module_manager(), name)


class ModuleManager(metaclass=_ModuleRuntimeProxy):
    """应用层兼容门面，实例调用返回组合根装配的模块管理器。"""

    def __new__(cls) -> ModuleRuntime:
        """返回实际模块管理器，不复制运行态注册表。"""
        return get_module_manager()


__all__ = [
    "ModuleManager",
    "ModuleRuntime",
    "configure_module_runtime",
    "get_module_manager",
    "reset_module_runtime",
]
