"""进程内后台任务登记与关停语义测试。"""

import asyncio
import threading

import pytest

from app.runtime.tasks import TaskRegistry


def test_task_registry_removes_completed_task() -> None:
    """正常完成的任务应自动退出登记表，避免长期持有请求对象。"""

    async def scenario() -> None:
        registry = TaskRegistry()
        release = asyncio.Event()

        async def worker() -> None:
            """等待测试释放信号。"""
            await release.wait()

        task = registry.create(worker(), owner="test.completed")
        assert [record.owner for record in registry.records] == ["test.completed"]

        release.set()
        await task
        await asyncio.sleep(0)

        assert registry.records == ()

    asyncio.run(scenario())


def test_task_registry_cancels_tasks_and_rejects_late_registration() -> None:
    """关停应取消存量任务，并拒绝在资源释放阶段继续产生新任务。"""

    async def scenario() -> None:
        registry = TaskRegistry()
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def worker() -> None:
            """记录任务收到取消信号。"""
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        task = registry.create(worker(), owner="test.shutdown")
        await started.wait()
        assert await registry.shutdown(timeout_seconds=1.0) is True

        assert task.cancelled()
        assert cancelled.is_set()
        assert registry.records == ()

        async def late_worker() -> None:
            """模拟关停开始后到达的晚任务。"""

        with pytest.raises(RuntimeError, match="正在关闭"):
            registry.create(late_worker(), owner="test.late")

    asyncio.run(scenario())


def test_task_registry_runs_sync_function_and_tracks_until_completion() -> None:
    """同步任务应在线程池执行，并在真实完成前保留 owner 记录。"""

    async def scenario() -> None:
        registry = TaskRegistry()
        release = asyncio.Event()

        def worker(value: int) -> int:
            """返回传入值，验证参数和结果没有被登记器改写。"""
            return value

        task = registry.create_sync(worker, 7, owner="test.sync")
        assert [record.owner for record in registry.records] == ["test.sync"]
        assert await task == 7
        await asyncio.sleep(0)
        assert registry.records == ()

        release.set()

    asyncio.run(scenario())


def test_task_registry_keeps_timed_out_sync_owner_until_real_completion() -> None:
    """同步线程超过关停预算后仍应保留 owner，不能把包装任务取消成伪完成。"""

    async def scenario() -> None:
        registry = TaskRegistry()
        started = threading.Event()
        release = threading.Event()
        reports: list[dict[str, object]] = []
        loop = asyncio.get_running_loop()
        previous_handler = loop.get_exception_handler()
        loop.set_exception_handler(lambda _, context: reports.append(context))

        def worker() -> None:
            """模拟无法由 asyncio 取消、需要外部资源自行结束的同步工作。"""
            started.set()
            release.wait()

        try:
            task = registry.create_sync(worker, owner="test.sync-timeout")
            assert await asyncio.to_thread(started.wait, 1.0)

            assert await registry.shutdown(timeout_seconds=0.001) is False

            assert not task.done()
            assert [record.owner for record in registry.records] == [
                "test.sync-timeout"
            ]
            assert reports[-1]["owners"] == ("test.sync-timeout",)

            release.set()
            await task
            await asyncio.sleep(0)
            assert registry.records == ()
        finally:
            release.set()
            loop.set_exception_handler(previous_handler)

    asyncio.run(scenario())


def test_task_registry_keeps_stubborn_cancelled_task_visible() -> None:
    """协程清理超过预算时只收一次取消，并在最终退出后自动清理。"""

    async def scenario() -> None:
        registry = TaskRegistry()
        started = asyncio.Event()
        cleanup_started = asyncio.Event()
        release = asyncio.Event()
        cancellation_count = 0
        reports: list[dict[str, object]] = []
        loop = asyncio.get_running_loop()
        previous_handler = loop.get_exception_handler()
        loop.set_exception_handler(lambda _, context: reports.append(context))

        async def worker() -> None:
            """模拟收到取消后仍必须完成的异步清理。"""
            nonlocal cancellation_count
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancellation_count += 1
                cleanup_started.set()
                await release.wait()

        task = registry.create(worker(), owner="test.stubborn")
        await started.wait()
        try:
            assert await registry.shutdown(timeout_seconds=0.001) is False

            assert cleanup_started.is_set()
            assert not task.done()
            assert cancellation_count == 1
            assert [record.owner for record in registry.records] == [
                "test.stubborn"
            ]
            assert reports[-1]["owners"] == ("test.stubborn",)

            assert await registry.shutdown(timeout_seconds=0.001) is False
            assert not task.done()
            assert cancellation_count == 1
            assert len(reports) == 1

            release.set()
            await task
            await asyncio.sleep(0)
            assert registry.records == ()
        finally:
            release.set()
            loop.set_exception_handler(previous_handler)

    asyncio.run(scenario())
