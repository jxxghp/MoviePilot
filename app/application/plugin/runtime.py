"""插件运行时目录的应用层端口。"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import nullcontext
from typing import Any, ContextManager, Protocol

from app.schemas.types import SystemConfigKey


PLUGIN_MUTATION_SYSTEM_CONFIG_KEYS = frozenset(
    {
        SystemConfigKey.UserInstalledPlugins,
        SystemConfigKey.PluginInstances,
        SystemConfigKey.PluginFolders,
    }
)


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


def plugin_system_config_mutation(
    key: str | SystemConfigKey | None,
) -> ContextManager[None]:
    """仅为会改变插件运行态所有权的系统配置取得 mutation lease。"""
    if key is None:
        return nullcontext()
    try:
        normalized_key = key if isinstance(key, SystemConfigKey) else SystemConfigKey(key)
    except ValueError:
        return nullcontext()
    if normalized_key not in PLUGIN_MUTATION_SYSTEM_CONFIG_KEYS:
        return nullcontext()
    return get_plugin_manager().mutation(f"更新插件系统配置 {normalized_key.value}")
