"""Scheduler 声明、catalog 与 execution state 测试。"""

import asyncio
from unittest.mock import MagicMock

import pytest

from app.application.scheduling import (
    JobCatalog,
    JobExecutionState,
    JobOverlapPolicy,
    JobRecoveryPolicy,
    JobSpec,
)


def test_job_spec_exports_complete_legacy_state() -> None:
    """每个声明必须显式暴露 owner、overlap、timeout、manual 和 recovery。"""
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
    """重复 job ID 在 APScheduler 注册前即失败。"""
    spec = JobSpec("same", "相同", MagicMock(), "test")

    with pytest.raises(ValueError, match="不得重复"):
        JobCatalog([spec, spec])


def test_execution_state_skips_overlap_and_records_terminal_state() -> None:
    """统一 execution state 保持旧的跳过重入并记录失败终态。"""
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
    """声明的 timeout 必须取消协程并向 Facade 抛出超时。"""
    cancelled = asyncio.Event()

    async def wait_forever() -> None:
        """模拟只能由取消信号结束的协程任务。"""
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    with pytest.raises(asyncio.TimeoutError):
        await JobExecutionState.await_result(wait_forever(), timeout_seconds=0.01)

    assert cancelled.is_set()


def test_manual_job_declares_restart_policy() -> None:
    """只允许人工触发的任务必须显式声明重启后不自动补跑。"""
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
