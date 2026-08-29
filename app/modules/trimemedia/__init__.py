"""飞牛影视宿主模块的惰性兼容入口。"""

from importlib import import_module
from typing import Any


_EXPORTS = {
    "TrimeMediaModule": ("app.modules.trimemedia.module", "TrimeMediaModule"),
}


def __getattr__(name: str) -> Any:
    """按需解析历史包级导出，并保持模块类的原始反射路径。"""
    contract = _EXPORTS.get(name)
    if contract is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, symbol_name = contract
    value = getattr(import_module(module_name), symbol_name)
    if name == "TrimeMediaModule":
        value.__module__ = __name__
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """向交互式工具公开兼容符号而不提前加载实现。"""
    return sorted({*globals(), *_EXPORTS})


__all__ = ["TrimeMediaModule"]
