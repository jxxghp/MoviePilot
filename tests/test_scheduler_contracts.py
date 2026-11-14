"""
Scheduler 声明、catalog、execution state 与协程执行预算语义测试。
"""

import asyncio
import threading
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from app.application.scheduling import (
    JobCatalog,
    JobExecutionState,
    JobOverlapPolicy,
    JobRecoveryPolicy,
    JobSpec,
)
from app.scheduler import execution as scheduler_execution
from app.scheduler.facade import Scheduler
from app.scheduler.registry import ExecutionRegistry


def test_job_spec_exports_complete_legacy_state() -> None:
    """
    每个声明必须显式暴露 owner、overlap、timeout、manual 和 recovery。
    """
    state = JobSpec(
        "outbox",
        "恢复副作用",
        MagicMock(),
        "outbox",
        timeout_seconds=30,
        recovery=JobRecoveryPolicy.DURABLE_QUEUE,
    ).to_runtime_state()

    assert state["owner"] == "outbox"
    assert state["overlap"] == JobOverlapPolicy.SKIP.value
    assert state["timeout_seconds"] == 30
    assert state["manual"] is False
    assert state["recovery"] == JobRecoveryPolicy.DURABLE_QUEUE.value


def test_job_catalog_rejects_duplicate_ids() -> None:
    """
    重复 job ID 在 APScheduler 注册前即失败。
    """
    spec = JobSpec("same", "相同", MagicMock(), "test")

    with pytest.raises(ValueError, match="不得重复"):
        JobCatalog([spec, spec])


def test_execution_state_skips_overlap_and_records_terminal_state() -> None:
    """
    统一 execution state 保持旧的跳过重入并记录失败终态。
    """
    state = JobSpec("job", "任务", MagicMock(), "test").to_runtime_state()

    assert JobExecutionState.begin(state, "start") is True
    assert JobExecutionState.begin(state, "second") is False
    JobExecutionState.finish(state, "finish", "failed")

    assert state["running"] is False
    assert state["last_started_at"] == "start"
    assert state["last_finished_at"] == "finish"
    assert state["last_error"] == "failed"


@pytest.mark.asyncio
async def test_execution_state_cancels_coroutine_after_timeout() -> None:
    """
    声明的 timeout 必须取消协程并向 Facade 抛出超时。
    """
    cancelled = asyncio.Event()

    async def wait_forever() -> None:
        """
        模拟只能由取消信号结束的协程任务。
        """
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    with pytest.raises(asyncio.TimeoutError):
        await JobExecutionState.await_result(wait_forever(), timeout_seconds=0.01)

    assert cancelled.is_set()


def test_manual_job_declares_restart_policy() -> None:
    """
    只允许人工触发的任务必须显式声明重启后不自动补跑。
    """
    state = JobSpec(
        "manual",
        "人工任务",
        MagicMock(),
        "runtime",
        manual=True,
        recovery=JobRecoveryPolicy.MANUAL_ONLY,
    ).to_runtime_state()

    assert state["manual"] is True
    assert state["recovery"] == JobRecoveryPolicy.MANUAL_ONLY.value


async def _coro_job() -> None:
    """
    协程任务样本，只提供可内省的异步签名。
    """
    return None


def _resolved_timeout(spec: JobSpec, default_seconds: int) -> Any:
    """
    用最小 Scheduler owner 走一遍预算补齐路径，返回运行状态里的最终超时值。

    :param spec: 待解析的任务声明
    :param default_seconds: 模拟的全局协程默认预算
    :return: 补齐后的 `timeout_seconds`
    """
    scheduler = object.__new__(Scheduler)
    scheduler._lock = threading.RLock()
    scheduler._registry = ExecutionRegistry(scheduler._lock)
    state = spec.to_runtime_state()
    with patch.object(
        scheduler_execution,
        "get_runtime_setting",
        return_value=default_seconds,
    ):
        scheduler._assign_job_generation(spec.job_id, state)
    return state["timeout_seconds"]


def test_declared_zero_timeout_keeps_job_unlimited() -> None:
    """
    显式声明 0 秒的协程任务不得继承全局预算：托管消费者取消不掉，套上总时长上限只会
    把正常长轮次记成超时失败。
    """
    spec = JobSpec(
        "queue",
        "队列恢复",
        _coro_job,
        "subscription",
        timeout_seconds=0,
        recovery=JobRecoveryPolicy.DURABLE_QUEUE,
    )

    assert _resolved_timeout(spec, default_seconds=3600) is None


def test_undeclared_coroutine_timeout_inherits_global_default() -> None:
    """
    未声明预算的协程任务继承全局默认，同步任务保持不限时。
    """
    assert _resolved_timeout(
        JobSpec("coro", "协程任务", _coro_job, "runtime"),
        default_seconds=120,
    ) == 120
    assert _resolved_timeout(
        JobSpec("sync", "同步任务", MagicMock(), "runtime"),
        default_seconds=120,
    ) is None


def test_positive_declared_timeout_wins_over_global_default() -> None:
    """
    正数声明是本次执行的总时长上限，不被全局默认覆盖。
    """
    assert _resolved_timeout(
        JobSpec("bounded", "有界任务", _coro_job, "runtime", timeout_seconds=30),
        default_seconds=3600,
    ) == 30


def test_timeout_error_keeps_descriptive_message() -> None:
    """
    `asyncio` 超时异常的文本为空，日志、系统通知和事件都必须带上可读的超时说明。
    """
    job = {"name": "恢复订阅搜索队列", "timeout_seconds": 3600}
    helper = MagicMock()
    with (
        patch.object(scheduler_execution.logger, "error") as error_log,
        patch.object(scheduler_execution, "_message_helper_factory", new=lambda: helper),
        patch.object(scheduler_execution.eventmanager, "send_event") as send_event,
    ):
        Scheduler._handle_job_error(
            job_id="subscribe_search_queue",
            job=job,
            error=asyncio.TimeoutError(),
            detail="任务执行超时（3600 秒）",
        )

    expected = "任务执行超时（3600 秒）"
    assert expected in error_log.call_args.args[0]
    assert helper.put.call_args.kwargs["message"] == expected
    assert send_event.call_args.args[1]["error"] == expected


def test_job_error_falls_back_to_exception_text() -> None:
    """
    未提供说明时沿用异常文本，普通异常的错误链路不变。
    """
    helper = MagicMock()
    with (
        patch.object(scheduler_execution.logger, "error") as error_log,
        patch.object(scheduler_execution, "_message_helper_factory", new=lambda: helper),
        patch.object(scheduler_execution.eventmanager, "send_event"),
    ):
        Scheduler._handle_job_error(
            job_id="job",
            job={"name": "普通任务"},
            error=RuntimeError("站点无响应"),
        )

    assert "站点无响应" in error_log.call_args.args[0]
    assert helper.put.call_args.kwargs["message"] == "站点无响应"
