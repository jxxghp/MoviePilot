"""文件管理模块的惰性兼容入口。

宿主能力清单和历史调用方继续使用 ``app.modules.filemanager:FileManagerModule``；
实现移入 ``module`` 后，包初始化不再反向加载传输处理器和存储实现。
"""

from importlib import import_module
from typing import Any


_EXPORTS = {
    "DirectoryHelper": ("app.modules.filemanager.module", "DirectoryHelper"),
    "FileManagerModule": ("app.modules.filemanager.module", "FileManagerModule"),
    "StorageBase": ("app.modules.filemanager.storages", "StorageBase"),
    "TransHandler": ("app.modules.filemanager.transhandler", "TransHandler"),
    "settings": ("app.modules.filemanager.module", "settings"),
}


def __getattr__(name: str) -> Any:
    """按需解析旧包级导出，并缓存解析结果。"""
    contract = _EXPORTS.get(name)
    if contract is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, symbol_name = contract
    value = getattr(import_module(module_name), symbol_name)
    if name == "FileManagerModule":
        # 保持插件反射、Pickle 和能力入口依赖的历史类路径。
        value.__module__ = __name__
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """向交互式工具公开兼容符号而不提前导入实现。"""
    return sorted({*globals(), *_EXPORTS})


__all__ = [
    "DirectoryHelper",
    "FileManagerModule",
    "StorageBase",
    "TransHandler",
    "settings",
]
