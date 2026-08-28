"""文件整理 Chain 的惰性稳定公开入口。"""

from importlib import import_module
from typing import Any

_EXPORTS = {
    "TransferChain": ("app.chain.transfer.facade", "TransferChain"),
    "task_lock": ("app.chain.transfer.queue", "task_lock"),
}


def __getattr__(name: str) -> Any:
    """首次访问时解析稳定 ABI，避免包初始化形成反向依赖。"""
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


__all__ = ["TransferChain", "task_lock"]
