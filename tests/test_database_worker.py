"""数据库短事务 worker 的容量、取消与关闭合同测试。"""

import asyncio
import concurrent.futures
import threading
from concurrent.futures import Future
from typing import Any
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
        """占住唯一线程，以便验证排队额度与拒绝边界。"""
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
        """保留一个运行中事务，使下一次调用稳定处于排队状态。"""
        started.set()
        release.wait(1)

    running = asyncio.create_task(worker.run(block))
    await asyncio.to_thread(started.wait)
    queued = asyncio.create_task(worker.run(queued_executed.set))
    await asyncio.sleep(0)
    queued.cancel()
    with pytest.raises(asyncio.CancelledError):
        await queued
    assert worker.snapshot().queued == 0
    assert worker.snapshot().running == 1

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
        """取消期间继续持有真实事务，直到调用方明确允许收口。"""
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
    assert worker.snapshot().queued == 0
    assert worker.snapshot().running == 0
    await worker.shutdown()


@pytest.mark.asyncio
async def test_shutdown_rejects_new_work_and_waits_for_running_work() -> None:
    """关闭期间不接收新任务，并等待已开始的操作结束。"""
    worker = DatabaseWorker(max_workers=1, capacity=2)
    await worker.start()
    started = threading.Event()
    release = threading.Event()

    def operation() -> None:
        """保持已开始任务运行，使测试能观察关闭期间的拒绝策略。"""
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
        """让事务跨过关闭期限，验证执行器仍保有它的 owner。"""
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
        """控制开始和结束两个观测点，验证指标最终归零。"""
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


@pytest.mark.asyncio
@pytest.mark.parametrize("fails", [False, True])
async def test_completion_releases_capacity_before_publishing_result(monkeypatch, fails: bool) -> None:
    """线程完成与事件循环发布之间的握手确保结果或异常不能早于容量回收。"""
    worker = DatabaseWorker(max_workers=1, capacity=1)
    await worker.start()
    loop = asyncio.get_running_loop()
    operation_started = threading.Event()
    release_operation = threading.Event()
    release_completion = threading.Event()
    observed = loop.create_future()
    original_schedule = worker._schedule_completion

    def pause_completion(completed: Future, item: Any) -> None:
        """模拟线程在完成 callback 中暂时失去调度，结果转发不能越过这道边界。"""
        def observe() -> None:
            """在所属事件循环核验 Future 可见性，而非等待一个猜测性的延迟。"""
            if not observed.done():
                wrapped, _ = worker._futures[completed]
                observed.set_result((wrapped.done(), worker.snapshot()))

        loop.call_soon_threadsafe(observe)
        try:
            assert release_completion.wait(5)
        finally:
            original_schedule(completed, item)

    def operation() -> str:
        """先等完成回调注册完毕，再交付成功值或原始业务异常。"""
        operation_started.set()
        assert release_operation.wait(5)
        if fails:
            raise ValueError("operation failed")
        return "committed-result"

    monkeypatch.setattr(worker, "_schedule_completion", pause_completion)
    task = asyncio.create_task(worker.run(operation))
    try:
        assert await asyncio.to_thread(operation_started.wait, 5)
        release_operation.set()
        published, pending = await asyncio.wait_for(observed, timeout=5)
        assert pending.running == 1 and pending.queued == 0
        assert published is False
        release_completion.set()
        if fails:
            with pytest.raises(ValueError, match="operation failed"):
                await task
        else:
            assert await task == "committed-result"
        assert worker.snapshot().running == 0
        assert worker.snapshot().queued == 0
        assert worker._futures == {}
        assert await worker.run(lambda: "capacity available") == "capacity available"
    finally:
        release_operation.set()
        release_completion.set()
        await asyncio.gather(task, return_exceptions=True)
        await worker.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "error", "cancelled"])
async def test_completion_callback_handles_future_finished_before_registration(monkeypatch, outcome: str) -> None:
    """submit 已返回终态 Future 时 callback 会同步触发，仍需先登记 owner 再有序释放。"""
    worker = DatabaseWorker(max_workers=1, capacity=1)
    await worker.start()
    submitted = []

    def submit(function, *args: Any) -> Future:
        """模拟极快线程在 submit 返回前结束，保留真实 _execute 的开始记账路径。"""
        future: Future = Future()
        if outcome == "cancelled":
            future.cancel()
        else:
            future.set_running_or_notify_cancel()
            try:
                result = function(*args)
            except BaseException as error:
                future.set_exception(error)
            else:
                future.set_result(result)
        assert future.done()
        submitted.append(future)
        return future

    def operation() -> str:
        """测试业务异常必须原样传递，完成顺序不能吞掉失败。"""
        if outcome == "error":
            raise ValueError("immediate failure")
        return "immediate result"

    monkeypatch.setattr(worker._executor, "submit", submit)
    try:
        if outcome == "error":
            with pytest.raises(ValueError, match="immediate failure"):
                await worker.run(operation)
        elif outcome == "cancelled":
            with pytest.raises(asyncio.CancelledError):
                await worker.run(operation)
        else:
            assert await worker.run(operation) == "immediate result"
        assert len(submitted) == 1
        assert worker.snapshot().queued == 0 and worker.snapshot().running == 0
        assert worker._futures == {}
    finally:
        await worker.shutdown()


@pytest.mark.asyncio
async def test_finished_future_cannot_publish_while_first_callback_is_pending(monkeypatch) -> None:
    """Future 先变 FINISHED 再执行 callback；晚注册的转发不能绕过仍未排队的完成记账。"""
    worker = DatabaseWorker(max_workers=1, capacity=1)
    await worker.start()
    loop = asyncio.get_running_loop()
    release_operation = threading.Event()
    first_callback_entered = threading.Event()
    release_callback = threading.Event()
    original_submit = worker._executor.submit
    native_futures = []

    def submit(function, *args: Any) -> Future:
        """保留真实线程/Future，只控制首次回调注册与后续注册之间的合法时序。"""
        future = original_submit(function, *args)
        original_add = future.add_done_callback
        first = True

        def add(callback) -> None:
            """首个回调已阻塞且 Future 已结束后，才允许主线程继续注册其余回调。"""
            nonlocal first
            if not first:
                original_add(callback)
                return
            first = False

            def paused(completed: Future) -> None:
                """线程端在 FINISHED 状态暂停，后注册回调仍可能在调用线程立即执行。"""
                first_callback_entered.set()
                assert release_callback.wait(5)
                callback(completed)

            original_add(paused)
            release_operation.set()
            assert first_callback_entered.wait(5)
            assert future.done()

        monkeypatch.setattr(future, "add_done_callback", add)
        native_futures.append(future)
        return future

    def operation() -> str:
        """用输入握手保证首次回调先注册，任务随后才结束。"""
        assert release_operation.wait(5)
        return "finished value"

    monkeypatch.setattr(worker._executor, "submit", submit)
    task = asyncio.create_task(worker.run(operation))
    try:
        assert await asyncio.to_thread(first_callback_entered.wait, 5)
        barrier = loop.create_future()
        loop.call_soon(barrier.set_result, None)
        await barrier
        state = worker._futures.get(native_futures[0])
        if state is not None:
            assert state[0].done() is False
        assert task.done() is False
        release_callback.set()
        assert await task == "finished value"
        assert worker.snapshot().queued == 0 and worker.snapshot().running == 0
    finally:
        release_operation.set()
        release_callback.set()
        await asyncio.gather(task, return_exceptions=True)
        await worker.shutdown()


class _DerivedCancellation(concurrent.futures.CancelledError):
    """并发 Future 异常子类不属于 asyncio 的精确类型翻译范围。"""


class _DerivedInvalidState(concurrent.futures.InvalidStateError):
    """业务声明的状态错误子类必须保持原对象，而非统一换成 asyncio 异常。"""


@pytest.mark.asyncio
@pytest.mark.parametrize(("source_type", "expected_type"), [
    (concurrent.futures.CancelledError, asyncio.CancelledError),
    (concurrent.futures.InvalidStateError, asyncio.InvalidStateError),
    (ValueError, ValueError),
    (_DerivedCancellation, _DerivedCancellation),
    (_DerivedInvalidState, _DerivedInvalidState),
])
async def test_result_publication_preserves_future_exception_contract(source_type, expected_type) -> None:
    """精确翻译原有 Future 异常，保留参数、业务 traceback 和其他异常的对象身份。"""
    worker = DatabaseWorker(max_workers=1, capacity=1)
    await worker.start()
    source = source_type("native future error", 42)

    def operation() -> None:
        """在真实 worker 线程抛出异常，验证跨线程发布后的 traceback。"""
        raise source

    try:
        with pytest.raises(expected_type) as raised:
            await worker.run(operation)
        assert type(raised.value) is expected_type
        assert raised.value.args == source.args
        if source_type is expected_type:
            assert raised.value is source
        else:
            assert raised.value is not source
        traceback = raised.value.__traceback__
        codes = []
        while traceback is not None:
            codes.append(traceback.tb_frame.f_code)
            traceback = traceback.tb_next
        assert operation.__code__ in codes
        assert worker.snapshot().queued == 0 and worker.snapshot().running == 0
        assert worker._futures == {}
        assert await worker.run(lambda: "still usable") == "still usable"
    finally:
        await worker.shutdown()
