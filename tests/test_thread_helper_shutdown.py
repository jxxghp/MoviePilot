import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.runtime import thread as thread_module
from app.runtime.thread import ThreadHelper


def _new_thread_helper() -> ThreadHelper:
    """构造不进入全局 Singleton 的隔离线程池 owner。"""
    helper = object.__new__(ThreadHelper)
    helper.__init__()
    return helper


@pytest.mark.parametrize("use_legacy_pool", [False, True])
def test_thread_helper_shutdown_is_bounded_and_retryable(use_legacy_pool):
    """宿主 submit 和旧 pool.submit 都必须进入同一个可重试关闭 owner。"""
    helper = _new_thread_helper()
    entered = threading.Event()
    release = threading.Event()

    def blocked_work() -> str:
        """模拟无法由线程池强制取消的同步任务。"""
        entered.set()
        release.wait()
        return "done"

    assert isinstance(helper.pool, ThreadPoolExecutor)
    submit = helper.pool.submit if use_legacy_pool else helper.submit
    future = submit(blocked_work)
    try:
        assert entered.wait(timeout=1)
        started_at = time.monotonic()
        assert helper.shutdown(timeout=0.01) is False
        assert time.monotonic() - started_at < 1
        assert not future.done()

        with pytest.raises(RuntimeError):
            helper.submit(lambda: None)
        with pytest.raises(RuntimeError):
            helper.pool.submit(lambda: None)
    finally:
        release.set()

    assert future.result(timeout=1) == "done"
    assert helper.shutdown(timeout=1) is True


def test_thread_helper_shutdown_preserves_queued_work():
    """关闭封口不得取消已接受的排队任务，保持历史完成语义。"""
    executor = thread_module._OwnedThreadPoolExecutor(max_workers=1)
    entered = threading.Event()
    release = threading.Event()

    def blocked_work() -> str:
        """占用唯一 worker，确保后一任务仍在队列中。"""
        entered.set()
        release.wait()
        return "first"

    first = executor.submit(blocked_work)
    queued = executor.submit(lambda: "queued")
    try:
        assert entered.wait(timeout=1)
        assert executor.shutdown_bounded(timeout=0.01) is False
        assert not queued.cancelled()
    finally:
        release.set()

    assert first.result(timeout=1) == "first"
    assert queued.result(timeout=1) == "queued"
    assert executor.shutdown_bounded(timeout=1) is True


def test_thread_helper_shutdown_waits_for_worker_after_future_completion():
    """Future 已完成但用户回调仍阻塞时不得误报 worker 已收敛。"""
    executor = thread_module._OwnedThreadPoolExecutor(max_workers=1)
    work_release = threading.Event()
    callback_entered = threading.Event()
    callback_release = threading.Event()

    future = executor.submit(lambda: work_release.wait())

    def blocked_done_callback(_future) -> None:
        """模拟 Future 终态之后仍占用 worker 的第三方完成回调。"""
        callback_entered.set()
        callback_release.wait()

    future.add_done_callback(blocked_done_callback)
    work_release.set()
    try:
        assert callback_entered.wait(timeout=1)
        assert future.done()
        started_at = time.monotonic()
        assert executor.shutdown_bounded(timeout=0.01) is False
        assert time.monotonic() - started_at < 1
    finally:
        callback_release.set()

    assert executor.shutdown_bounded(timeout=1) is True
