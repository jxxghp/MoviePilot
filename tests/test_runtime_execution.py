"""运行时同步 worker 的取消与容量合同回归。"""

import asyncio
import threading

import pytest
from anyio.to_thread import current_default_thread_limiter

from app.runtime.execution import run_in_threadpool_to_completion


@pytest.mark.asyncio
async def test_threadpool_capacity_is_held_until_cancelled_call_finishes() -> None:
    """调用方取消后，执行令牌必须由真实同步调用持有到终态。"""
    limiter = current_default_thread_limiter()
    original_capacity = limiter.total_tokens
    release = threading.Event()
    first_started = threading.Event()
    second_started = threading.Event()

    def blocking_call(started: threading.Event) -> None:
        started.set()
        release.wait()

    limiter.total_tokens = 1
    first = asyncio.create_task(
        run_in_threadpool_to_completion(blocking_call, first_started)
    )
    second = None
    try:
        while not first_started.is_set():
            await asyncio.sleep(0)

        first.cancel()
        await asyncio.sleep(0)
        first.cancel()
        await asyncio.sleep(0)

        assert first.done() is False
        assert limiter.borrowed_tokens == 1

        second = asyncio.create_task(
            run_in_threadpool_to_completion(blocking_call, second_started)
        )
        await asyncio.sleep(0.01)
        assert second_started.is_set() is False

        release.set()
        with pytest.raises(asyncio.CancelledError):
            await first
        await second
    finally:
        release.set()
        if not first.done():
            await asyncio.gather(first, return_exceptions=True)
        if second is not None and not second.done():
            await asyncio.gather(second, return_exceptions=True)
        limiter.total_tokens = original_capacity


@pytest.mark.asyncio
async def test_cancelled_threadpool_call_preserves_worker_failure_as_cause() -> None:
    """调用方取消优先返回，线程终态异常仍保留为诊断原因。"""
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    loop_errors: list[dict] = []
    release = threading.Event()
    started = threading.Event()

    def failing_call() -> None:
        started.set()
        release.wait()
        raise ValueError("worker failed")

    task = asyncio.create_task(run_in_threadpool_to_completion(failing_call))
    while not started.is_set():
        await asyncio.sleep(0)

    task.cancel()
    release.set()

    loop.set_exception_handler(lambda _loop, context: loop_errors.append(context))
    try:
        with pytest.raises(asyncio.CancelledError) as error_info:
            await task
        await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(previous_handler)
    assert isinstance(error_info.value.__cause__, ValueError)
    assert loop_errors == []
