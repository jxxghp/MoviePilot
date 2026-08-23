"""Scheduler 任务句柄、generation 与 AgentTask reservation 回归。"""

import asyncio
import threading

import pytest

from app import scheduler as scheduler_module
from app.runtime.config import global_vars
from app.scheduler import Scheduler


class _ProgressStub:
    """隔离 scheduler 生命周期测试的同步进度后端。"""

    def __init__(self, _key: str) -> None:
        """接收进度键但不连接外部后端。"""

    def start(self) -> None:
        """记录进度开始。"""

    def update(self, **_kwargs) -> None:
        """忽略中间进度。"""


class _AsyncProgressStub:
    """隔离 scheduler 生命周期测试的异步进度后端。"""

    def __init__(self, _key: str) -> None:
        """接收进度键但不连接外部后端。"""

    async def get(self):
        """返回空的历史进度。"""
        return None

    async def update(self, **_kwargs) -> None:
        """忽略中间进度。"""

    async def end(self, **_kwargs) -> None:
        """记录终态但不访问外部缓存。"""


def _scheduler(job_id: str, func) -> Scheduler:
    """构造已启动但不拥有 APScheduler 线程的实例。"""
    scheduler = object.__new__(Scheduler)
    scheduler._scheduler = None
    scheduler._event = threading.Event()
    scheduler._lock = threading.RLock()
    scheduler._jobs = {
        job_id: {
            "name": "生命周期测试",
            "provider_name": "测试",
            "func": func,
            "running": False,
            "_generation": 1,
        }
    }
    scheduler._lifecycle_state = "running"
    scheduler._handles = {}
    scheduler._job_generations = {job_id: 1}
    scheduler._agent_task_reservations = {}
    return scheduler


@pytest.mark.anyio
async def test_stop_async_cancels_and_awaits_scheduler_owned_job(monkeypatch) -> None:
    """关闭后已投递协程必须取消并完成收尾，不得遗留 owner 句柄。"""
    started = asyncio.Event()
    cleaned = asyncio.Event()

    async def job():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    monkeypatch.setattr(scheduler_module, "ProgressHelper", _ProgressStub)
    monkeypatch.setattr(scheduler_module, "AsyncProgressHelper", _AsyncProgressStub)
    scheduler = _scheduler("lifecycle-job", job)

    assert scheduler.start("lifecycle-job") is True
    await asyncio.wait_for(started.wait(), timeout=1)
    assert scheduler._handles

    await scheduler.stop_async()

    assert cleaned.is_set()
    assert scheduler._jobs["lifecycle-job"]["running"] is False
    assert scheduler._jobs["lifecycle-job"]["last_error"] == "任务已取消"
    assert scheduler._handles == {}
    assert scheduler._lifecycle_state == "stopped"


@pytest.mark.anyio
async def test_submit_to_loop_tracks_internal_progress_or_finish_tasks() -> None:
    """进度和收尾协程也必须归 Scheduler 所有并可在关闭时收口。"""
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def pending() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    scheduler = _scheduler("internal-task", lambda: None)
    scheduler._submit_to_loop(
        pending(),
        job_id="internal-task",
        generation=1,
    )

    await asyncio.wait_for(started.wait(), timeout=1)
    assert len(scheduler._handles) == 1

    await scheduler.stop_async()

    assert cancelled.is_set()
    assert scheduler._handles == {}


@pytest.mark.anyio
async def test_sync_job_callback_and_finish_handles_are_owned(monkeypatch) -> None:
    """同步任务回投的进度与收尾句柄都必须纳入关闭收口。"""
    update_started = asyncio.Event()
    finish_started = asyncio.Event()
    gate = asyncio.Event()
    cancelled = 0

    class BlockingProgress:
        """让进度和收尾停在异步后端，便于验证 owner registry。"""

        def __init__(self, _key: str) -> None:
            pass

        async def update(self, **_kwargs) -> None:
            nonlocal cancelled
            update_started.set()
            try:
                await gate.wait()
            except asyncio.CancelledError:
                cancelled += 1
                raise

        async def get(self):
            nonlocal cancelled
            finish_started.set()
            try:
                await gate.wait()
            except asyncio.CancelledError:
                cancelled += 1
                raise
            return None

        async def end(self, **_kwargs) -> None:
            pass

    monkeypatch.setattr(scheduler_module, "ProgressHelper", _ProgressStub)
    monkeypatch.setattr(scheduler_module, "AsyncProgressHelper", BlockingProgress)
    monkeypatch.setattr(global_vars, "CURRENT_EVENT_LOOP", asyncio.get_running_loop())

    def job(progress_callback) -> None:
        progress_callback(value=50)

    scheduler = _scheduler("callback-handles", job)
    await asyncio.to_thread(scheduler.start, "callback-handles")
    await asyncio.wait_for(
        asyncio.gather(update_started.wait(), finish_started.wait()),
        timeout=1,
    )

    assert len(scheduler._handles) == 2

    await scheduler.stop_async()

    assert cancelled == 2
    assert scheduler._handles == {}


@pytest.mark.anyio
async def test_stale_generation_cannot_finish_replaced_job(monkeypatch) -> None:
    """旧 generation 收尾不得改写同 ID 的新任务状态或进度。"""
    monkeypatch.setattr(scheduler_module, "AsyncProgressHelper", _AsyncProgressStub)
    scheduler = _scheduler("generation-job", lambda: None)
    old_job = scheduler._jobs["generation-job"]
    old_job["running"] = True
    new_job = {
        "name": "新一代",
        "provider_name": "测试",
        "running": True,
        "_generation": 2,
    }
    scheduler._jobs["generation-job"] = new_job

    await scheduler._Scheduler__finish_job(
        job_id="generation-job",
        job=old_job,
        generation=1,
        success=True,
    )

    assert new_job["running"] is True
    assert "last_finished_at" not in new_job
    assert old_job["running"] is True


def test_agent_task_manual_start_has_single_reservation() -> None:
    """并发手动触发同一 AgentTask 时只能有一个调用获得 reservation。"""
    scheduler = _scheduler("agent-task-1", lambda: None)
    scheduler._jobs["agent-task-1"].update(
        name="AgentTask",
        owner="agent",
    )
    entered = threading.Event()
    release = threading.Event()
    results = []

    def start(*_args, **_kwargs):
        entered.set()
        release.wait(timeout=1)
        return True

    scheduler.start = start

    first = threading.Thread(
        target=lambda: results.append(scheduler.start_agent_task(1)),
    )
    first.start()
    assert entered.wait(timeout=1)
    second = scheduler.start_agent_task(1)
    release.set()
    first.join(timeout=1)

    assert second is False
    assert results == [True]
    assert scheduler._agent_task_reservations == {}


def test_scheduler_rejects_new_submission_after_stop() -> None:
    """进入 stopping/stopped 后不得再从旧 scheduler 提交任务。"""
    scheduler = _scheduler("stopped-job", lambda: None)
    scheduler._lifecycle_state = "stopping"

    assert scheduler.start("stopped-job") is False
    assert scheduler._jobs["stopped-job"]["running"] is False
