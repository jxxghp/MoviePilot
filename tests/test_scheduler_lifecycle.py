"""Scheduler 任务句柄、generation 与 AgentTask reservation 回归。"""

import asyncio
import gc
import inspect
import threading
import warnings

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

    def get(self):
        """返回空的历史进度。"""
        return None


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
    scheduler._active_job_generations = {}
    scheduler._agent_task_reservations = {}
    return scheduler


def test_internal_loop_submission_requires_job_owner() -> None:
    """Scheduler 内部协程桥接不得再提供省略 job owner 的游离提交分支。"""
    parameter = inspect.signature(Scheduler._submit_to_loop).parameters["job_id"]

    assert parameter.default is inspect.Parameter.empty
    assert parameter.annotation is str


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
async def test_stop_during_final_progress_does_not_mark_completed_job_unsubmitted(
        monkeypatch,
) -> None:
    """业务协程已完成后，取消最终进度写入不得改写任务执行结果。"""
    finish_started = asyncio.Event()

    class BlockingFinishProgress(_AsyncProgressStub):
        """把任务停在最终进度读取阶段。"""

        async def get(self):
            finish_started.set()
            await asyncio.Event().wait()

    async def job() -> None:
        return None

    monkeypatch.setattr(scheduler_module, "ProgressHelper", _ProgressStub)
    monkeypatch.setattr(
        scheduler_module,
        "AsyncProgressHelper",
        BlockingFinishProgress,
    )
    scheduler = _scheduler("final-progress-stop", job)

    assert scheduler.start("final-progress-stop") is True
    await asyncio.wait_for(finish_started.wait(), timeout=1)

    await scheduler.stop_async()

    assert scheduler._jobs["final-progress-stop"]["running"] is False
    assert scheduler._jobs["final-progress-stop"]["last_error"] is None
    assert scheduler._handles == {}
    assert scheduler._active_job_generations == {}

    monkeypatch.setattr(
        scheduler_module,
        "AsyncProgressHelper",
        _AsyncProgressStub,
    )
    scheduler._lifecycle_state = "running"
    assert scheduler.start("final-progress-stop") is True

    async def wait_until_finished() -> None:
        while scheduler._handles or scheduler._active_job_generations:
            await asyncio.sleep(0)

    await asyncio.wait_for(wait_until_finished(), timeout=1)


@pytest.mark.anyio
async def test_foreign_loop_submission_runs_on_main_loop_and_finishes_before_stop(
        monkeypatch,
) -> None:
    """自建事件循环提交的任务仍由应用主循环拥有并完成取消收尾。"""
    main_loop = asyncio.get_running_loop()
    started = asyncio.Event()
    cancelling = asyncio.Event()
    cleanup_release = asyncio.Event()
    execution_loop = None

    async def job() -> None:
        nonlocal execution_loop
        execution_loop = asyncio.get_running_loop()
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelling.set()
            await cleanup_release.wait()
            raise

    monkeypatch.setattr(scheduler_module, "ProgressHelper", _ProgressStub)
    monkeypatch.setattr(scheduler_module, "AsyncProgressHelper", _AsyncProgressStub)
    monkeypatch.setattr(global_vars, "CURRENT_EVENT_LOOP", main_loop)
    scheduler = _scheduler("foreign-loop-job", job)

    def submit_from_foreign_loop() -> bool:
        async def submit() -> bool:
            return scheduler.start("foreign-loop-job")

        return asyncio.run(submit())

    assert await asyncio.to_thread(submit_from_foreign_loop) is True
    await asyncio.wait_for(started.wait(), timeout=1)
    assert execution_loop is main_loop

    stop_task = asyncio.create_task(scheduler.stop_async())
    await asyncio.wait_for(cancelling.wait(), timeout=1)
    await asyncio.sleep(0)
    assert not stop_task.done()
    assert scheduler._lifecycle_state == "stopping"

    stop_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stop_task
    assert scheduler._handles
    assert scheduler._lifecycle_state == "stopping"

    cleanup_release.set()

    async def wait_until_released() -> None:
        while scheduler._handles:
            await asyncio.sleep(0)

    await asyncio.wait_for(wait_until_released(), timeout=1)
    await scheduler.stop_async()
    assert scheduler._handles == {}
    assert scheduler._lifecycle_state == "stopped"


@pytest.mark.anyio
async def test_cross_thread_submission_is_registered_before_stop_snapshot(
        monkeypatch,
) -> None:
    """跨线程提交与 owner 登记必须对关闭快照表现为同一原子操作。"""
    main_loop = asyncio.get_running_loop()
    registration_entered = threading.Event()
    registration_release = threading.Event()

    async def job() -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(scheduler_module, "ProgressHelper", _ProgressStub)
    monkeypatch.setattr(scheduler_module, "AsyncProgressHelper", _AsyncProgressStub)
    monkeypatch.setattr(global_vars, "CURRENT_EVENT_LOOP", main_loop)
    scheduler = _scheduler("atomic-submit", job)
    register_handle = scheduler._register_handle

    def delayed_register(**kwargs) -> bool:
        registration_entered.set()
        registration_release.wait(timeout=1)
        return register_handle(**kwargs)

    monkeypatch.setattr(scheduler, "_register_handle", delayed_register)
    submit_thread = threading.Thread(target=scheduler.start, args=("atomic-submit",))
    submit_thread.start()
    assert await asyncio.to_thread(registration_entered.wait, 1)

    stop_result = []
    stop_thread = threading.Thread(target=lambda: stop_result.append(scheduler._begin_stop()))
    stop_thread.start()
    await asyncio.sleep(0.02)
    assert stop_thread.is_alive()

    registration_release.set()
    await asyncio.to_thread(submit_thread.join, 1)
    await asyncio.to_thread(stop_thread.join, 1)
    assert not submit_thread.is_alive()
    assert not stop_thread.is_alive()
    assert len(stop_result[0][1]) == 1

    for handle in stop_result[0][1]:
        scheduler._cancel_handle(handle)
    await scheduler._await_cancelled_handles(stop_result[0][1])


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
    await asyncio.wait_for(update_started.wait(), timeout=1)
    await asyncio.sleep(0)

    assert len(scheduler._handles) == 2
    assert not finish_started.is_set()

    await scheduler.stop_async()

    assert cancelled == 1
    assert scheduler._handles == {}


@pytest.mark.anyio
async def test_stale_progress_cannot_update_replaced_job(monkeypatch) -> None:
    """旧 generation 的延迟进度不得写入新注册的同 ID 任务。"""
    updates = []

    class RecordingProgress:
        def __init__(self, _key: str) -> None:
            pass

        async def update(self, **kwargs) -> None:
            updates.append(kwargs)

    monkeypatch.setattr(scheduler_module, "AsyncProgressHelper", RecordingProgress)
    scheduler = _scheduler("generation-progress", lambda: None)
    old_job = scheduler._jobs["generation-progress"]
    callback = scheduler._Scheduler__build_progress_callback(
        "generation-progress",
        old_job,
    )
    scheduler._jobs["generation-progress"] = {
        "name": "新一代",
        "provider_name": "测试",
        "running": True,
        "_generation": 2,
    }

    callback(value=42, text="旧进度")
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert updates == []
    assert scheduler._handles == {}


@pytest.mark.anyio
async def test_final_progress_waits_for_pending_update(monkeypatch) -> None:
    """任务终态必须等待已提交的进度回调，避免 running 快照迟到覆盖。"""
    update_started = asyncio.Event()
    allow_update = asyncio.Event()
    finished = asyncio.Event()
    writes = []

    class BlockingProgress(_AsyncProgressStub):
        """把中间进度写停在终态收尾之前。"""

        async def update(self, **_kwargs) -> None:
            update_started.set()
            await allow_update.wait()
            writes.append("update")

        async def end(self, **_kwargs) -> None:
            writes.append("end")
            finished.set()

    async def job(progress_callback) -> None:
        progress_callback(value=100, text="业务处理完成")

    monkeypatch.setattr(scheduler_module, "ProgressHelper", _ProgressStub)
    monkeypatch.setattr(scheduler_module, "AsyncProgressHelper", BlockingProgress)
    scheduler = _scheduler("progress-order", job)

    assert scheduler.start("progress-order") is True
    await asyncio.wait_for(update_started.wait(), timeout=1)
    await asyncio.sleep(0)

    assert writes == []
    assert not finished.is_set()

    allow_update.set()
    await asyncio.wait_for(finished.wait(), timeout=1)

    assert writes == ["update", "end"]


@pytest.mark.anyio
async def test_replaced_job_keeps_active_state_without_stale_progress(monkeypatch) -> None:
    """同 ID 新 generation 显示真实运行态，但不继承旧任务进度详情。"""
    detail = {}

    class RecordingProgress:
        def __init__(self, _key: str) -> None:
            pass

        def start(self) -> None:
            pass

        def update(self, **kwargs) -> None:
            detail.update(kwargs)

        def get(self):
            return detail

    class RecordingAsyncProgress:
        def __init__(self, _key: str) -> None:
            pass

        async def get(self):
            return detail

    monkeypatch.setattr(scheduler_module, "ProgressHelper", RecordingProgress)
    monkeypatch.setattr(
        scheduler_module,
        "AsyncProgressHelper",
        RecordingAsyncProgress,
    )
    scheduler = _scheduler("generation-cache", lambda: None)
    old_job = scheduler._Scheduler__prepare_job("generation-cache")
    assert old_job is not None
    assert detail["data"]["_generation"] == 1

    scheduler._jobs["generation-cache"] = {
        "name": "新一代",
        "provider_name": "测试",
        "running": False,
        "_generation": 2,
    }

    progress = scheduler.get_progress("generation-cache")
    assert progress is not None
    assert progress.status == "running"
    assert progress.enable is True
    assert progress.value == 0
    assert "_generation" not in progress.data
    async_progress = await scheduler.aget_progress("generation-cache")
    assert async_progress is not None
    assert async_progress.status == "running"
    assert async_progress.enable is True
    assert async_progress.value == 0
    assert "_generation" not in async_progress.data


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


@pytest.mark.anyio
async def test_config_reload_does_not_restart_scheduler_during_shutdown() -> None:
    """系统关闭开始后到达的配置事件不得重新打开调度入口。"""
    scheduler = _scheduler("shutdown-reload", lambda: None)
    scheduler._lifecycle_state = "stopping"
    scheduler.init = lambda **_kwargs: pytest.fail("关闭阶段不得重新初始化调度器")

    await scheduler.on_config_changed()

    assert scheduler._lifecycle_state == "stopping"


@pytest.mark.anyio
async def test_concurrent_config_reload_waits_for_old_scheduler_shutdown(
        monkeypatch,
) -> None:
    """并发配置事件合并为一次重建，旧调度线程池结束前不得启动新实例。"""
    shutdown_started = threading.Event()
    shutdown_release = threading.Event()

    class BlockingScheduler:
        running = True

        @staticmethod
        def remove_all_jobs() -> None:
            pass

        @staticmethod
        def shutdown() -> None:
            shutdown_started.set()
            shutdown_release.wait(timeout=1)

    scheduler = _scheduler("reload-once", lambda: None)
    scheduler._scheduler = BlockingScheduler()
    init_calls = 0

    def init(**_kwargs) -> None:
        nonlocal init_calls
        init_calls += 1
        scheduler._lifecycle_state = "running"

    monkeypatch.setattr(scheduler, "init", init)
    first = asyncio.create_task(scheduler.on_config_changed())
    assert await asyncio.to_thread(shutdown_started.wait, 1)

    await scheduler.on_config_changed()
    assert init_calls == 0
    assert scheduler._lifecycle_state == "reloading"

    shutdown_release.set()
    await asyncio.wait_for(first, timeout=1)
    assert init_calls == 1
    assert scheduler._lifecycle_state == "running"


@pytest.mark.anyio
async def test_config_reload_preserves_overlap_guard_across_job_generations(
        monkeypatch,
) -> None:
    """热重载替换任务定义后，同 ID 旧任务结束前不得启动新 generation。"""
    started = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()
    run_count = 0

    async def job() -> None:
        nonlocal run_count
        run_count += 1
        if run_count == 1:
            started.set()
            await release.wait()
            finished.set()

    monkeypatch.setattr(scheduler_module, "ProgressHelper", _ProgressStub)
    monkeypatch.setattr(scheduler_module, "AsyncProgressHelper", _AsyncProgressStub)
    scheduler = _scheduler("reload-overlap", job)

    class ActiveScheduler:
        """提供列表接口所需的最小 APScheduler 状态。"""

        running = True

        @staticmethod
        def get_jobs() -> list:
            """当前用例只关注正在运行任务，不提供后续计划。"""
            return []

    def init(**_kwargs) -> None:
        replacement = {
            "name": "生命周期测试",
            "provider_name": "测试",
            "func": job,
            "running": False,
        }
        scheduler._assign_job_generation("reload-overlap", replacement)
        scheduler._jobs = {"reload-overlap": replacement}
        scheduler._scheduler = ActiveScheduler()
        scheduler._lifecycle_state = "running"

    monkeypatch.setattr(scheduler, "init", init)

    assert scheduler.start("reload-overlap") is True
    await asyncio.wait_for(started.wait(), timeout=1)
    await scheduler.on_config_changed()

    progress = scheduler.get_progress("reload-overlap")
    assert progress is not None
    assert progress.status == "running"
    assert progress.enable is True
    listed = scheduler.list()
    assert len(listed) == 1
    assert listed[0].id == "reload-overlap"
    assert listed[0].status == "正在运行"
    assert scheduler.start("reload-overlap") is False
    assert run_count == 1
    assert len(scheduler._handles) == 1

    release.set()
    await asyncio.wait_for(finished.wait(), timeout=1)

    async def wait_until_released() -> None:
        while scheduler._active_job_generations or scheduler._handles:
            await asyncio.sleep(0)

    await asyncio.wait_for(wait_until_released(), timeout=1)
    assert scheduler.start("reload-overlap") is True

    async def wait_until_second_run_finishes() -> None:
        while scheduler._active_job_generations or scheduler._handles:
            await asyncio.sleep(0)

    await asyncio.wait_for(wait_until_second_run_finishes(), timeout=1)
    assert run_count == 2


def test_stop_between_prepare_and_submission_releases_active_generation(
        monkeypatch,
) -> None:
    """关闭插入准备与提交之间时，不得遗留未实际运行的 generation。"""
    calls = 0
    scheduler = _scheduler("stop-race", None)

    async def job() -> None:
        nonlocal calls
        calls += 1

    scheduler._jobs["stop-race"]["func"] = job
    original_prepare = scheduler._Scheduler__prepare_job

    def prepare_then_stop(job_id: str):
        prepared = original_prepare(job_id)
        scheduler._begin_stop()
        return prepared

    monkeypatch.setattr(scheduler, "_Scheduler__prepare_job", prepare_then_stop)

    assert scheduler.start("stop-race") is False
    assert calls == 0
    assert scheduler._handles == {}
    assert scheduler._active_job_generations == {}
    assert scheduler._jobs["stop-race"]["running"] is False
    assert scheduler._jobs["stop-race"]["last_error"] == "任务未提交"

    monkeypatch.setattr(scheduler, "_Scheduler__prepare_job", original_prepare)
    scheduler._lifecycle_state = "running"
    assert scheduler.start("stop-race") is True
    assert calls == 1
    assert scheduler._active_job_generations == {}


@pytest.mark.anyio
async def test_cross_thread_rejection_closes_unstarted_business_coroutine(
        monkeypatch,
) -> None:
    """跨线程提交被关闭门禁拒绝时，包装与业务协程都必须释放。"""
    main_loop = asyncio.get_running_loop()
    prepared = threading.Event()
    release = threading.Event()
    calls = 0
    scheduler = _scheduler("cross-thread-stop-race", None)

    async def job() -> None:
        nonlocal calls
        calls += 1

    scheduler._jobs["cross-thread-stop-race"]["func"] = job
    monkeypatch.setattr(global_vars, "CURRENT_EVENT_LOOP", main_loop)
    original_prepare = scheduler._Scheduler__prepare_job

    def prepare_then_wait(job_id: str):
        result = original_prepare(job_id)
        prepared.set()
        release.wait(timeout=1)
        return result

    monkeypatch.setattr(scheduler, "_Scheduler__prepare_job", prepare_then_wait)

    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always", RuntimeWarning)
        start_task = asyncio.create_task(
            asyncio.to_thread(scheduler.start, "cross-thread-stop-race")
        )
        assert await asyncio.to_thread(prepared.wait, 1)
        scheduler._begin_stop()
        release.set()
        assert await asyncio.wait_for(start_task, timeout=1) is False
        gc.collect()

    assert calls == 0
    assert scheduler._active_job_generations == {}
    assert scheduler._handles == {}
    assert not any("was never awaited" in str(item.message) for item in captured)


def test_cancelled_cross_thread_proxy_waits_for_target_loop_cleanup(
        monkeypatch,
) -> None:
    """跨线程代理提前取消后，真实完成信号必须等待目标循环清理。"""
    target_loop = asyncio.new_event_loop()
    loop_blocked = threading.Event()
    loop_release = threading.Event()
    loop_drained = threading.Event()
    loop_errors = []
    loop_thread = threading.Thread(target=target_loop.run_forever)
    loop_thread.start()

    def block_target_loop() -> None:
        loop_blocked.set()
        loop_release.wait(timeout=1)

    target_loop.set_exception_handler(
        lambda _loop, context: loop_errors.append(context)
    )
    target_loop.call_soon_threadsafe(block_target_loop)
    assert loop_blocked.wait(timeout=1)

    scheduler = _scheduler("cancel-before-start", None)
    calls = 0

    async def business() -> None:
        nonlocal calls
        calls += 1

    scheduler._jobs["cancel-before-start"]["func"] = business
    monkeypatch.setattr(global_vars, "CURRENT_EVENT_LOOP", target_loop)

    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always", RuntimeWarning)
        try:
            assert scheduler.start("cancel-before-start") is True
            scheduler_handle = next(iter(scheduler._handles.values()))
            scheduler._cancel_handle(scheduler_handle)
            assert not scheduler_handle.completion.done()

            loop_release.set()
            target_loop.call_soon_threadsafe(loop_drained.set)
            assert loop_drained.wait(timeout=1)
            assert scheduler_handle.completion.done()
            gc.collect()
        finally:
            target_loop.call_soon_threadsafe(target_loop.stop)
            loop_thread.join(timeout=1)
            target_loop.close()

    assert calls == 0
    assert loop_errors == []
    assert scheduler._active_job_generations == {}
    assert scheduler._handles == {}
    assert not any("was never awaited" in str(item.message) for item in captured)
