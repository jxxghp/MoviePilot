"""线程池 worker 转入空闲时交还分配器线程缓存，连续任务之间不刷新。"""

from __future__ import annotations

import threading

import pytest

from app.runtime import execution
from app.runtime.execution import IdleCacheReleasingThreadPoolExecutor, OwnedThreadPoolExecutor


@pytest.fixture
def flushes(monkeypatch) -> list[int]:
    """记录每次刷新发生在哪个线程。"""
    calls: list[int] = []
    monkeypatch.setattr(
        execution,
        "flush_thread_allocator_cache",
        lambda: calls.append(threading.get_ident()) or True,
    )
    return calls


def test_worker_flushes_only_when_queue_drains(flushes) -> None:
    """排队中的任务之间不刷新，最后一个任务结束、队列清空后由 worker 自己刷新一次。"""
    gate = threading.Event()
    worker_ids: list[int] = []
    with IdleCacheReleasingThreadPoolExecutor(max_workers=1) as pool:
        blocker = pool.submit(gate.wait)
        queued = [pool.submit(lambda: worker_ids.append(threading.get_ident())) for _ in range(3)]
        gate.set()
        blocker.result(timeout=5)
        for future in queued:
            future.result(timeout=5)

    assert len(flushes) == 1
    assert flushes == [worker_ids[-1]]
    assert flushes[0] != threading.get_ident()


def test_results_and_exceptions_propagate(flushes) -> None:
    """包装不改变任务返回值与异常，失败的任务同样在空闲前刷新。"""
    with IdleCacheReleasingThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(lambda value: value * 2, 21).result(timeout=5) == 42
        failing = pool.submit(lambda: 1 / 0)
        with pytest.raises(ZeroDivisionError):
            failing.result(timeout=5)

    assert len(flushes) == 2


def test_owned_executor_releases_idle_worker_cache(flushes) -> None:
    """共享线程池 owner 继承空闲刷新，并保留自身的 Future 追踪与关闭合同。"""
    pool = OwnedThreadPoolExecutor(max_workers=2)
    assert pool.submit(lambda: "done").result(timeout=5) == "done"

    assert pool.shutdown_bounded(timeout=5) is True
    assert len(flushes) == 1
