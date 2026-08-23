"""进程内后台任务登记与关停语义测试。"""

import asyncio

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
        await registry.shutdown(timeout_seconds=1.0)

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
