"""插件契约与运行时管理器的惰性稳定公开入口。"""

from importlib import import_module
from typing import Any

_EXPORTS = {
    "ModuleManager": ("app.sdk.plugin.manager", "ModuleManager"),
    "PluginChain": ("app.sdk.plugin.base", "PluginChain"),
    "PluginManager": ("app.sdk.plugin.manager", "PluginManager"),
    "_PluginBase": ("app.sdk.plugin.base", "_PluginBase"),
}


def __getattr__(name: str) -> Any:
    """首次访问时解析稳定插件符号，避免导入契约基类连带拉起管理器。"""
    contract = _EXPORTS.get(name)
    if contract is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, symbol_name = contract
    value = getattr(import_module(module_name), symbol_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """向交互式工具暴露稳定公开面。"""
    return sorted({*globals(), *_EXPORTS})


__all__ = ["ModuleManager", "PluginChain", "PluginManager", "_PluginBase"]
