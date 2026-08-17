"""Schema 根包的惰性兼容导出入口。

公开符号清单由 ``scripts/schema/exports.py`` 从各 schema 子模块生成。
旧的 ``app.schemas.X`` 与 ``from app.schemas import X`` 路径保持不变，但仅在首次
访问具体符号时加载其所有者模块，避免任意 schema 导入触发完整模型图。
"""

from importlib import import_module as _import_module
from typing import Any as _Any

from app.schemas.exports import SCHEMA_EXPORTS as _SCHEMA_EXPORTS


def __getattr__(name: str) -> _Any:
    """按生成清单惰性解析并缓存 schema 公开符号。"""
    contract = _SCHEMA_EXPORTS.get(name)
    if contract is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, symbol_name = contract
    value = getattr(_import_module(module_name), symbol_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """返回兼容公开面，供 IDE、文档与交互式检查使用。"""
    return sorted({*globals(), *_SCHEMA_EXPORTS})


__all__ = sorted(_SCHEMA_EXPORTS)
