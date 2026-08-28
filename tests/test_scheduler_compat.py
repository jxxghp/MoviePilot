"""Scheduler 旧插件 ABI 与 concrete 单例身份测试。"""

import importlib
import inspect

from app.scheduler import Scheduler, SchedulerChain
from app.scheduler.facade import Scheduler as ConcreteScheduler


def test_scheduler_package_root_keeps_legacy_type_identity() -> None:
    """旧静态与动态导入必须解析为同一个 concrete 类型。"""
    dynamic = getattr(importlib.import_module("app.scheduler"), "Scheduler")

    assert Scheduler is ConcreteScheduler
    assert dynamic is ConcreteScheduler
    assert Scheduler.__module__ == "app.scheduler"
    assert SchedulerChain.__module__ == "app.scheduler"


def test_scheduler_legacy_methods_keep_plugin_call_shapes() -> None:
    """旧插件使用的四个方法继续保留原调用参数形态。"""
    start = inspect.signature(Scheduler.start)
    remove = inspect.signature(Scheduler.remove_plugin_job)

    assert list(start.parameters) == ["self", "job_id", "args", "kwargs"]
    assert list(remove.parameters) == ["self", "pid", "job_id"]
    assert remove.parameters["job_id"].default is None
    assert callable(Scheduler.list)
    assert callable(Scheduler.update_plugin_job)


def test_scheduler_legacy_constructor_reuses_single_state_owner() -> None:
    """兼容构造必须复用同一 concrete 单例和私有状态桥。"""
    first = Scheduler()
    second = Scheduler()

    assert first is second
    assert first._jobs is second._jobs
    assert first._lock is second._lock
    assert first._scheduler is second._scheduler
