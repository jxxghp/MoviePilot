"""同步与异步防抖生命周期测试。"""

import asyncio

import pytest

from app.runtime.debounce import AsyncDebouncer


@pytest.mark.asyncio
async def test_async_debouncer_cancel_waits_for_replaced_task_cleanup() -> None:
    """公开取消必须等待已被后续调用替换的任务完成取消清理。"""
    first_started = asyncio.Event()
    first_cancelled = asyncio.Event()
    allow_first_cleanup = asyncio.Event()

    async def delayed_work(value: str) -> None:
        """让首个调用在取消后停留，暴露 retired task 的终态时序。"""
        if value != "first":
            await asyncio.Event().wait()
            return
        first_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            first_cancelled.set()
            await allow_first_cleanup.wait()
            raise

    debouncer = AsyncDebouncer(delayed_work, interval=0)
    await debouncer("first")
    await first_started.wait()

    await debouncer("second")
    await first_cancelled.wait()
    cancel_task = asyncio.create_task(debouncer.cancel())
    await asyncio.sleep(0)

    assert cancel_task.done() is False
    allow_first_cleanup.set()
    await asyncio.wait_for(cancel_task, timeout=1)
    assert debouncer.task is None
    assert debouncer._retired_tasks == set()


@pytest.mark.asyncio
async def test_async_debouncer_cancel_is_idempotent_without_active_task() -> None:
    """没有活动任务时重复取消应立即返回并保持冷却状态关闭。"""

    async def no_op() -> None:
        """提供不会被实际调度的异步函数。"""

    debouncer = AsyncDebouncer(no_op, interval=1, leading=True)

    await debouncer.cancel()
    await debouncer.cancel()

    assert debouncer.task is None
    assert debouncer.is_cooling_down is False
