"""下载 Chain 的惰性稳定公开入口。"""

from importlib import import_module
from typing import Any

# 只向静态导入检查器声明稳定公开名；运行时仍由 __getattr__ 惰性解析。
DownloadChain: Any

_EXPORTS = {
    "DownloadChain": ("app.chain.download.facade", "DownloadChain"),
}


def __getattr__(name: str) -> Any:
    """首次访问时解析稳定 DownloadChain 类型。"""
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


__all__ = ["DownloadChain"]
