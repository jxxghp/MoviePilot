"""数据库短事务 worker 的容量、取消与关闭合同测试。"""

import asyncio
import threading
from unittest.mock import patch

import pytest

from app.db.worker import DatabaseWorker
from app.schemas.exception import (
    DatabaseWorkerClosedError,
    DatabaseWorkerOverloadedError,
)


@pytest.mark.asyncio
async def test_worker_requires_explicit_start() -> None:
    """构造对象不会隐式创建可执行线程池。"""
    worker = DatabaseWorker(max_workers=1, capacity=1)

    with pytest.raises(DatabaseWorkerClosedError):
        await worker.run(lambda: None)


@pytest.mark.asyncio
async def test_worker_rejects_work_beyond_running_and_queue_capacity() -> None:
    """运行与排队任务达到总容量后立即拒绝新任务。"""
    worker = DatabaseWorker(max_workers=1, capacity=2)
    await worker.start()
    started = threading.Event()
    release = threading.Event()

    def block() -> None:
        started.set()
        release.wait(1)

    running = asyncio.create_task(worker.run(block))
    await asyncio.to_thread(started.wait)
    queued = asyncio.create_task(worker.run(lambda: None))
    await asyncio.sleep(0)

    with pytest.raises(DatabaseWorkerOverloadedError):
        await worker.run(lambda: None)

    assert worker.snapshot().running == 1
    assert worker.snapshot().queued == 1
    assert worker.snapshot().rejected == 1

    release.set()
    await asyncio.gather(running, queued)
    await worker.shutdown()


@pytest.mark.asyncio
async def test_cancelling_queued_work_prevents_execution() -> None:
    """尚未取得线程的任务取消后不得执行数据库操作。"""
    worker = DatabaseWorker(max_workers=1, capacity=2)
    await worker.start()
    started = threading.Event()
    release = threading.Event()
    queued_executed = threading.Event()

    def block() -> None:
        started.set()
        release.wait(1)

    running = asyncio.create_task(worker.run(block))
    await asyncio.to_thread(started.wait)
    queued = asyncio.create_task(worker.run(queued_executed.set))
    await asyncio.sleep(0)
    queued.cancel()
    with pytest.raises(asyncio.CancelledError):
        await queued

    release.set()
    await running
    await worker.shutdown()

    assert queued_executed.is_set() is False
    assert worker.snapshot().queued == 0
    assert worker.snapshot().running == 0


@pytest.mark.asyncio
async def test_cancelling_running_work_waits_for_transaction_terminal_state() -> None:
    """线程内操作开始后，取消结果必须晚于操作的最终状态。"""
    worker = DatabaseWorker(max_workers=1, capacity=1)
    await worker.start()
    started = threading.Event()
    release = threading.Event()
    completed = threading.Event()

    def operation() -> None:
        started.set()
        release.wait(1)
        completed.set()

    task = asyncio.create_task(worker.run(operation))
    await asyncio.to_thread(started.wait)
    task.cancel()
    await asyncio.sleep(0.01)

    assert task.done() is False
    assert completed.is_set() is False

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert completed.is_set() is True
    await worker.shutdown()


@pytest.mark.asyncio
async def test_shutdown_rejects_new_work_and_waits_for_running_work() -> None:
    """关闭期间不接收新任务，并等待已开始的操作结束。"""
    worker = DatabaseWorker(max_workers=1, capacity=2)
    await worker.start()
    started = threading.Event()
    release = threading.Event()

    def operation() -> None:
        started.set()
        release.wait(1)

    running = asyncio.create_task(worker.run(operation))
    await asyncio.to_thread(started.wait)
    shutdown = asyncio.create_task(worker.shutdown())
    await asyncio.sleep(0)

    with pytest.raises(DatabaseWorkerClosedError):
        await worker.run(lambda: None)
    assert shutdown.done() is False

    release.set()
    await running
    await shutdown

    assert worker.snapshot().closing is True
    assert worker.snapshot().queued == 0
    assert worker.snapshot().running == 0


@pytest.mark.asyncio
async def test_shutdown_timeout_keeps_running_owner_until_transaction_finishes() -> None:
    """关闭超时应返回给生命周期编排，并保留执行器等待事务收敛。"""
    worker = DatabaseWorker(max_workers=1, capacity=1)
    await worker.start()
    started = threading.Event()
    release = threading.Event()

    def operation() -> None:
        started.set()
        release.wait(1)

    running = asyncio.create_task(worker.run(operation))
    await asyncio.to_thread(started.wait)

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(worker.shutdown(), timeout=0.01)

    assert worker.snapshot().closing is True
    assert worker._executor is not None

    release.set()
    await running
    await asyncio.sleep(0)
    await worker.shutdown()
    assert worker._executor is None


@pytest.mark.asyncio
async def test_worker_depth_metrics_emit_deltas_and_return_to_zero() -> None:
    """队列和运行量指标按增减量上报，不能把绝对值累加成漂移。"""
    worker = DatabaseWorker(max_workers=1, capacity=1)
    started = threading.Event()
    release = threading.Event()

    def operation() -> None:
        started.set()
        release.wait(1)

    with patch("app.db.worker.record_metric") as record_metric:
        await worker.start()
        running = asyncio.create_task(worker.run(operation))
        await asyncio.to_thread(started.wait)
        release.set()
        await running
        await worker.shutdown()

    queue_values = [
        call.args[1]
        for call in record_metric.call_args_list
        if call.args[0] == "db.worker.queue.depth"
    ]
    active_values = [
        call.args[1]
        for call in record_metric.call_args_list
        if call.args[0] == "db.worker.active"
    ]
    assert queue_values == [1.0, -1.0]
    assert active_values == [1.0, -1.0]
