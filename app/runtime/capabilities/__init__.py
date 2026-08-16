"""MoviePilot 内部能力运行时的惰性公共导出。"""

from __future__ import annotations

from importlib import import_module
from typing import Any


_EXPORT_MODULES = {
    "ActivationPolicy": "app.runtime.capabilities.model",
    "AdapterExecutionMode": "app.runtime.capabilities.model",
    "AsyncCapabilityAdapter": "app.runtime.capabilities.model",
    "CapabilityAdapterContractError": "app.runtime.capabilities.errors",
    "CapabilityAdapterModeError": "app.runtime.capabilities.errors",
    "CapabilityError": "app.runtime.capabilities.errors",
    "CapabilityLifecycleState": "app.runtime.capabilities.model",
    "CapabilityManifestError": "app.runtime.capabilities.errors",
    "CapabilityMaterializationState": "app.runtime.capabilities.model",
    "CapabilityObservation": "app.runtime.capabilities.model",
    "CapabilityOperationError": "app.runtime.capabilities.errors",
    "CapabilityRegistry": "app.runtime.capabilities.registry",
    "CapabilityRuntime": "app.runtime.capabilities.runtime",
    "CapabilityRuntimeClosedError": "app.runtime.capabilities.errors",
    "CapabilitySnapshot": "app.runtime.capabilities.model",
    "CapabilitySpec": "app.runtime.capabilities.model",
    "SelectorSchema": "app.runtime.capabilities.model",
    "SelectorSpec": "app.runtime.capabilities.model",
    "SyncCapabilityAdapter": "app.runtime.capabilities.model",
    "UnknownCapabilityError": "app.runtime.capabilities.errors",
}

__all__ = sorted(_EXPORT_MODULES)


def __getattr__(name: str) -> Any:
    """仅在显式访问公共符号时导入对应叶模块，并缓存 canonical 对象。"""
    module_name = _EXPORT_MODULES.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """向交互式工具公开稳定导出名，而不触发叶模块导入。"""
    return sorted(set(globals()) | set(__all__))
