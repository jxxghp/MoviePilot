import asyncio
import threading
from unittest.mock import Mock
from uuid import uuid4

import pytest

from app.runtime.config import global_vars
from app.runtime.scheduler import SchedulerEngine
from app.scheduler import Scheduler
from app.scheduler import composition as composition_module


def _build_scheduler(job_id, func):
    """构造不启动 APScheduler 的定时服务测试对象。"""
    scheduler = object.__new__(Scheduler)
    scheduler._lock = threading.RLock()
    scheduler._jobs = {
        job_id: {
            "name": "测试定时服务",
            "provider_name": "测试",
            "func": func,
            "running": False,
        }
    }
    return scheduler


def test_scheduler_records_live_and_completed_progress():
    """定时服务应在执行中更新进度，并在成功后收敛为 100%。"""
    job_id = f"test-success-{uuid4()}"
    snapshots = []

    def task(progress_callback):
        """上报一次中间进度。"""
        progress_callback(value=42, text="正在处理", data={"finished": 2})
        snapshots.append(scheduler.get_progress(job_id))

    scheduler = _build_scheduler(job_id, task)

    scheduler.start(job_id)

    assert snapshots[0].enable is True
    assert snapshots[0].value == 42
    assert snapshots[0].status == "running"
    assert snapshots[0].data["finished"] == 2
    progress = scheduler.get_progress(job_id)
    assert progress.enable is False
    assert progress.value == 100
    assert progress.status == "success"
    assert progress.success is True
    assert progress.started_at
    assert progress.finished_at


def test_scheduler_failure_preserves_last_progress(monkeypatch):
    """定时服务异常时应保留失败位置，而不是伪装成 100% 完成。"""
    job_id = f"test-failure-{uuid4()}"

    def task(progress_callback):
        """上报进度后抛出异常。"""
        progress_callback(value=37, text="处理失败")
        raise RuntimeError("预期失败")

    scheduler = _build_scheduler(job_id, task)
    monkeypatch.setattr(
        scheduler,
        "_SchedulerEngine__handle_job_error",
        lambda **kwargs: None,
    )

    scheduler.start(job_id)

    progress = scheduler.get_progress(job_id)
    assert progress.enable is False
    assert progress.value == 37
    assert progress.status == "failed"
    assert progress.success is False
    assert progress.error == "预期失败"


def test_scheduler_treats_standard_failure_result_as_failed():
    """返回 `(False, message)` 的定时服务应记录为业务失败。"""
    job_id = f"test-result-failure-{uuid4()}"

    def task(progress_callback):
        """返回标准失败结果。"""
        progress_callback(value=55, text="业务校验失败")
        return False, "业务失败"

    scheduler = _build_scheduler(job_id, task)

    scheduler.start(job_id)

    progress = scheduler.get_progress(job_id)
    assert progress.value == 55
    assert progress.status == "failed"
    assert progress.error == "业务失败"


def test_scheduler_runs_async_job_without_running_global_loop(monkeypatch):
    """全局事件循环未运行时，异步定时服务仍应正常执行并收敛进度。"""
    job_id = f"test-async-{uuid4()}"

    async def task(progress_callback):
        """上报异步任务进度。"""
        progress_callback(value=65, text="异步处理中")

    scheduler = _build_scheduler(job_id, task)
    target_loop = asyncio.new_event_loop()
    monkeypatch.setattr(global_vars, "CURRENT_EVENT_LOOP", target_loop)

    try:
        scheduler.start(job_id)
    finally:
        target_loop.close()

    progress = scheduler.get_progress(job_id)
    assert progress.enable is False
    assert progress.value == 100
    assert progress.status == "success"


def test_scheduler_runs_async_job_from_current_event_loop(monkeypatch):
    """在异步入口中手动触发定时服务时，不应嵌套调用 `asyncio.run`。"""
    job_id = f"test-current-loop-{uuid4()}"

    async def task(progress_callback):
        """在当前事件循环中上报进度。"""
        progress_callback(value=75, text="当前循环处理中")

    async def run_task():
        """从已运行的事件循环启动定时服务。"""
        scheduler.start(job_id)
        await asyncio.sleep(0)

    scheduler = _build_scheduler(job_id, task)
    target_loop = asyncio.new_event_loop()
    monkeypatch.setattr(global_vars, "CURRENT_EVENT_LOOP", target_loop)

    try:
        asyncio.run(run_task())
    finally:
        target_loop.close()

    progress = scheduler.get_progress(job_id)
    assert progress.enable is False
    assert progress.value == 100
    assert progress.status == "success"


def test_scheduler_records_cancelled_async_job_as_failed():
    """协程任务取消时运行时进度不得收敛为成功。"""
    job_id = f"test-cancelled-{uuid4()}"

    async def task():
        """模拟传播到调度器外壳的取消。"""
        raise asyncio.CancelledError

    async def run_task():
        job = scheduler._SchedulerEngine__prepare_job(job_id)
        with pytest.raises(asyncio.CancelledError):
            await scheduler._SchedulerEngine__run_coro_job(task(), job_id, job)

    scheduler = _build_scheduler(job_id, task)
    asyncio.run(run_task())

    progress = scheduler.get_progress(job_id)
    assert progress.enable is False
    assert progress.status == "failed"
    assert progress.success is False
    assert progress.error == "任务已取消"


def test_scheduler_returns_none_for_unknown_job():
    """未注册且无历史进度的定时服务应返回空。"""
    job_id = f"test-unknown-{uuid4()}"
    scheduler = object.__new__(Scheduler)
    scheduler._lock = threading.RLock()
    scheduler._jobs = {}

    assert scheduler.get_progress(job_id) is None


def test_engine_routes_job_failure_through_the_host_hook(monkeypatch):
    """引擎只经宿主钩子投递失败提示，自身不认识消息中心。"""
    job_id = f"test-hook-{uuid4()}"

    def task():
        """直接抛出异常。"""
        raise RuntimeError("预期失败")

    scheduler = _build_scheduler(job_id, task)
    notified = []
    monkeypatch.setattr(
        scheduler,
        "notify_job_failure",
        lambda title, message: notified.append((title, message)),
    )

    scheduler.start(job_id)

    assert notified == [("测试定时服务 执行失败", "预期失败")]


def test_engine_default_hook_does_not_reach_any_message_center():
    """引擎默认实现不投递任何提示，宿主策略由组合根重写。"""
    assert SchedulerEngine.notify_job_failure is not Scheduler.notify_job_failure
    assert SchedulerEngine().notify_job_failure(title="t", message="m") is None


def test_host_hook_puts_a_system_message(monkeypatch):
    """组合根把失败提示投递到消息中心的系统通道。"""
    message_helper = Mock()
    monkeypatch.setattr(
        composition_module, "MessageHelper", lambda: message_helper
    )

    object.__new__(Scheduler).notify_job_failure(title="标题", message="正文")

    message_helper.put.assert_called_once_with(
        title="标题", message="正文", role="system"
    )
