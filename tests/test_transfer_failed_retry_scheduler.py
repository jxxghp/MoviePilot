"""失败整理 AI 重试调度器的生命周期测试。"""

import asyncio
from unittest.mock import Mock, patch

import pytest

from app.application.transfer import FailedRetryScheduler


def test_retry_scheduler_close_cancels_buffered_timer_and_rejects_new_work():
    """关闭应取消尚未触发的 timer、清空缓冲并拒绝新增记录。"""

    async def exercise() -> None:
        """在独立事件循环内验证 timer 与关闭状态。"""
        scheduler = FailedRetryScheduler()
        scheduler.RETRY_TRANSFER_DEBOUNCE_SECONDS = 60
        await scheduler.schedule_retry(11, group_key="media:test")
        timer = scheduler._retry_transfer_timers["media:test"]

        await scheduler.close()
        await scheduler.close()

        assert timer.cancelled()
        assert scheduler._retry_transfer_buffer == {}
        assert scheduler._retry_transfer_timers == {}
        with pytest.raises(RuntimeError, match="正在关闭"):
            await scheduler.schedule_retry(12, group_key="media:test")

    asyncio.run(exercise())


def test_retry_scheduler_close_cancels_and_waits_for_active_flush_task():
    """关闭返回前应等待已经启动的 flush 任务完成取消收尾。"""

    async def exercise() -> None:
        """启动一个不会自行结束的 flush，并通过关闭流程取消它。"""
        scheduler = FailedRetryScheduler()
        scheduler.RETRY_TRANSFER_DEBOUNCE_SECONDS = 0
        started = asyncio.Event()
        stopped = asyncio.Event()

        async def blocking_flush(_group_key: str, _generation: int) -> None:
            """等待取消信号，并在 finally 中证明收尾已经完成。"""
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        scheduler._flush_retry_transfer = blocking_flush
        await scheduler.schedule_retry(11, group_key="media:test")
        await asyncio.wait_for(started.wait(), timeout=1)
        task = next(iter(scheduler._retry_transfer_tasks))

        await scheduler.close()

        assert stopped.is_set()
        assert task.cancelled()
        assert task.get_name() == "transfer.failed_retry.flush"
        assert scheduler._retry_transfer_tasks == set()

    asyncio.run(exercise())


def test_retry_scheduler_observes_unexpected_background_task_error():
    """flush 协程越过自身防线的异常仍应由任务 owner 统一观察。"""

    async def exercise() -> None:
        """让受管 flush 任务直接失败，并等待完成回调处理异常。"""
        scheduler = FailedRetryScheduler()
        scheduler.RETRY_TRANSFER_DEBOUNCE_SECONDS = 0

        async def failing_flush(_group_key: str, _generation: int) -> None:
            """模拟 flush 外层出现未处理异常。"""
            raise RuntimeError("flush failed")

        scheduler._flush_retry_transfer = failing_flush
        with patch("app.application.transfer.logger.error", Mock()) as log_error:
            await scheduler.schedule_retry(11, group_key="media:test")
            for _ in range(5):
                await asyncio.sleep(0)
                if scheduler._retry_transfer_tasks:
                    break
            assert scheduler._retry_transfer_tasks
            for _ in range(5):
                await asyncio.sleep(0)
                if not scheduler._retry_transfer_tasks:
                    break

        assert scheduler._retry_transfer_tasks == set()
        log_error.assert_called_once()
        assert "flush failed" in log_error.call_args.args[0]
        await scheduler.close()

    asyncio.run(exercise())


def test_retry_scheduler_old_flush_cannot_consume_renewed_generation():
    """旧 timer 已建 task 后的新失败应续期，不能被旧 flush 提前取走。"""

    async def exercise() -> None:
        """稳定复现 timer callback 与同组新 schedule 交错的窗口。"""
        scheduler = FailedRetryScheduler()
        scheduler.RETRY_TRANSFER_DEBOUNCE_SECONDS = 3600
        await scheduler.schedule_retry(11, group_key="media:test")
        old_timer = scheduler._retry_transfer_timers["media:test"]
        old_generation = scheduler._retry_transfer_generations["media:test"]

        # 模拟旧 timer callback 已进入事件循环，但 flush task 尚未取得分组锁。
        scheduler._start_retry_transfer_task("media:test", old_generation)
        await scheduler.schedule_retry(12, group_key="media:test")
        renewed_timer = scheduler._retry_transfer_timers["media:test"]
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert old_timer.cancelled()
        assert renewed_timer.cancelled() is False
        assert scheduler._retry_transfer_buffer["media:test"] == [11, 12]
        assert scheduler._retry_transfer_timers["media:test"] is renewed_timer
        assert scheduler._retry_transfer_tasks == set()

        await scheduler.close()
        assert renewed_timer.cancelled()

    asyncio.run(exercise())
