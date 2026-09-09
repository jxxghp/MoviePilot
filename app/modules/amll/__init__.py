"""AMLL capability manifest 使用的惰性模块入口。"""

from importlib import import_module
from typing import Any

_EXPORTS = {
    "AmllModule": ("app.modules.amll.module", "AmllModule"),
}
__all__ = ["AmllModule"]


def __getattr__(name: str) -> Any:
    """按宿主要求保留包级 capability 身份，实现与协议解析分别由子模块拥有。"""
    contract = _EXPORTS.get(name)
    if contract is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, symbol_name = contract
    implementation = getattr(import_module(module_name), symbol_name)
    implementation.__module__ = __name__
    globals()[name] = implementation
    return implementation
