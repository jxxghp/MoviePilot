"""
Scheduler 旧插件 ABI 与具体实现的单例身份测试。
"""

import importlib
import inspect

import pytest

# 调度器旧包根在运行时动态导出类型，此导入用于验证插件兼容入口。
from app.scheduler import Scheduler, SchedulerChain  # pylint: disable=no-name-in-module
from app.scheduler.facade import Scheduler as ConcreteScheduler


def test_scheduler_package_root_keeps_legacy_type_identity() -> None:
    """
    旧静态与动态导入必须解析为同一个具体实现类型。
    """
    dynamic = getattr(importlib.import_module("app.scheduler"), "Scheduler")

    assert Scheduler is ConcreteScheduler
    assert dynamic is ConcreteScheduler
    assert Scheduler.__module__ == "app.scheduler"
    assert SchedulerChain.__module__ == "app.scheduler"


def test_scheduler_package_root_rejects_unknown_attribute() -> None:
    """
    旧包根只暴露声明的兼容入口，未知属性应保持标准 AttributeError。
    """
    with pytest.raises(AttributeError, match="missing_scheduler_entrypoint"):
        getattr(importlib.import_module("app.scheduler"), "missing_scheduler_entrypoint")


def test_scheduler_legacy_methods_keep_plugin_call_shapes() -> None:
    """
    旧插件使用的四个方法继续保留原调用参数形态。
    """
    start = inspect.signature(Scheduler.start)
    remove = inspect.signature(Scheduler.remove_plugin_job)

    assert list(start.parameters) == ["self", "job_id", "args", "kwargs"]
    assert list(remove.parameters) == ["self", "pid", "job_id"]
    assert remove.parameters["job_id"].default is None
    assert callable(Scheduler.list)
    assert callable(Scheduler.update_plugin_job)


def test_scheduler_legacy_constructor_reuses_single_state_owner() -> None:
    """
    兼容构造必须复用同一具体实现单例和私有状态桥。
    """
    first = Scheduler()
    second = Scheduler()

    assert first is second
    assert first._jobs is second._jobs
    assert first._lock is second._lock
    assert first._scheduler is second._scheduler
