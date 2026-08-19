"""MoviePilot 离线诊断公开门面，具体对象按需解析。"""

from importlib import import_module
from typing import Any


_EXPORT_MODULES = {
    "DoctorFinding": "app.doctor.models",
    "DoctorReport": "app.doctor.models",
    "DoctorRunner": "app.doctor.runner",
    "run_doctor": "app.doctor.runner",
}


def __getattr__(name: str) -> Any:
    """首次访问公开诊断对象时只加载其所属实现模块。"""
    module_name = _EXPORT_MODULES.get(name)
    if module_name is None:
        raise AttributeError(f"module 'app.doctor' has no attribute {name!r}")
    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """让惰性公开对象继续支持交互式发现。"""
    return sorted(set(globals()) | set(_EXPORT_MODULES))


__all__ = [
    "DoctorFinding",
    "DoctorReport",
    "DoctorRunner",
    "run_doctor",
]
