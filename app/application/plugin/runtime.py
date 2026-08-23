"""插件运行时目录的应用层端口。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, ContextManager, Protocol

class PluginRuntime(Protocol):
    """声明入口层消费的插件宿主能力。"""

    def mutation(self, operation: str) -> ContextManager[None]:
        """为完整插件可变事务取得停机准入 lease。"""
        ...

    def __getattr__(self, name: str) -> Any:
        """允许兼容门面按既有 V3 方法名访问插件宿主能力。"""


PluginRuntimeProvider = Callable[[], PluginRuntime]


def _unconfigured_runtime() -> PluginRuntime:
    """拒绝在启动组合根完成前隐式创建 Runtime 管理器。"""
    raise RuntimeError("插件运行时尚未由启动组合根装配")


_runtime_provider: PluginRuntimeProvider = _unconfigured_runtime


def configure_plugin_runtime(provider: PluginRuntimeProvider) -> None:
    """由启动组合根注册插件运行时实例提供器。"""
    global _runtime_provider
    _runtime_provider = provider


def get_plugin_manager() -> PluginRuntime:
    """返回当前组合根提供的插件运行时能力。"""
    return _runtime_provider()
