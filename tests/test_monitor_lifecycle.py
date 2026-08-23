"""目录监控 owner 的生命周期与停机屏障回归。"""

import asyncio
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.monitor.monitor import Monitor
from app.monitor.recovery import RecoveryExecutor, RecoveryState
from app.foundation.singleton import SingletonClass
from app.startup.initializers.monitor import init_monitor, stop_monitor


def _build_monitor() -> Monitor:
    """构造绕过单例初始化、但具备完整生命周期字段的 Monitor 骨架。"""
    monitor = object.__new__(Monitor)
    monitor._lifecycle_lock = threading.RLock()
    monitor._owner_lock = threading.Lock()
    monitor._work_stop_event = threading.Event()
    monitor._shutdown_event = threading.Event()
    monitor._closed = False
    monitor._compensation_threads = {}
    monitor._scheduler_shutdown_thread = None
    monitor._scheduler_shutdown_succeeded = False
    monitor._scheduler = None
    monitor._watchers = []
    monitor._retired_watchers = []
    monitor._watcher_lock = threading.Lock()
    monitor._pending_locals = []
    monitor._alerted_paths = {}
    monitor._restart_marks = {}
    monitor._stable_cycles = {}
    monitor._isolated = {}
    monitor._pending_rebuild = {}
    monitor._recovery = RecoveryExecutor()
    monitor._dispatcher = MagicMock()
    return monitor


def _blocking_watcher(mon_path: Path):
    """构造只有显式 release 后才退出的 watcher 与控制事件。"""
    started = threading.Event()
    release = threading.Event()

    def run() -> None:
        """模拟无法被 stop event 唤醒的 FUSE watcher。"""
        started.set()
        release.wait()

    thread = threading.Thread(target=run, daemon=True, name="test-monitor-watcher")
    thread.start()
    assert started.wait(1)
    watcher = MagicMock()
    watcher.watch_path = mon_path
    watcher.stop.side_effect = lambda: None
    watcher.join.side_effect = thread.join
    watcher.is_alive.side_effect = thread.is_alive
    return watcher, thread, release


def test_close_budget_includes_lifecycle_lock_wait() -> None:
    """停机 deadline 必须先于生命周期锁等待建立，锁竞争超时后立即返回。"""
    monitor = _build_monitor()
    lock_acquired = threading.Event()
    release_lock = threading.Event()

    def hold_lifecycle_lock() -> None:
        """占住生命周期锁，模拟配置重载阻塞在挂载访问中。"""
        with monitor._lifecycle_lock:
            lock_acquired.set()
            release_lock.wait()

    holder = threading.Thread(target=hold_lifecycle_lock, daemon=True)
    holder.start()
    assert lock_acquired.wait(1)

    started_at = time.monotonic()
    try:
        assert monitor.close(timeout=0.02) is False
        assert time.monotonic() - started_at < 0.5
        assert monitor.lifecycle_closed is True
    finally:
        release_lock.set()
        holder.join(timeout=1)
    assert holder.is_alive() is False
    assert monitor.close(timeout=1) is True


def test_close_timeout_retains_watcher_and_seals_config_reload(tmp_path) -> None:
    """挂死 watcher 超时时保留句柄，且配置变化不能重开已封口生命周期。"""
    monitor = _build_monitor()
    watcher, thread, release = _blocking_watcher(tmp_path)
    monitor._watchers = [watcher]
    reload_monitor = MagicMock()
    monitor.init = reload_monitor

    try:
        assert monitor.close(timeout=0.02) is False
        assert monitor._watchers == [watcher]
        monitor._dispatcher.clear_pending.assert_not_called()

        monitor.on_config_changed()
        reload_monitor.assert_not_called()
    finally:
        release.set()
    assert monitor.close(timeout=1) is True
    thread.join(timeout=1)
    assert thread.is_alive() is False
    assert monitor._watchers == []
    monitor._dispatcher.clear_pending.assert_called_once_with()


def test_reopen_allows_only_an_explicit_new_lifespan() -> None:
    """close 后普通 init 被拒绝，显式 reopen 收敛旧 owner 后才允许重新初始化。"""
    monitor = _build_monitor()
    initialize = MagicMock(return_value=True)
    monitor._Monitor__initialize_monitors = initialize

    assert monitor.close(timeout=1) is True
    assert monitor.init(timeout=0) is False
    initialize.assert_not_called()

    assert monitor.reopen(timeout=1) is True
    assert monitor.init(timeout=1) is True
    initialize.assert_called_once_with()
    assert monitor.lifecycle_closed is False


def test_close_tracks_recovery_and_blocks_post_seal_dispatch() -> None:
    """恢复动作解冻后只能退出，不能越过封口继续派发整理重试。"""
    monitor = _build_monitor()
    entered = threading.Event()
    release = threading.Event()

    def blocking_local_retry() -> None:
        """模拟恢复线程阻塞在 FUSE 本地目录访问。"""
        entered.set()
        release.wait()

    monitor._Monitor__retry_pending_locals = blocking_local_retry
    result = monitor._recovery.run(
        {monitor.PENDING_KEY: monitor._Monitor__drive_pending}, timeout=0.01
    )
    assert entered.is_set()
    assert result == {monitor.PENDING_KEY: RecoveryState.TIMEOUT}

    try:
        assert monitor.close(timeout=0.02) is False
        assert len(monitor._recovery.running_threads()) == 1
        monitor._dispatcher.retry_pending.assert_not_called()
    finally:
        release.set()
    assert monitor.close(timeout=1) is True
    assert monitor._recovery.running_threads() == ()
    monitor._dispatcher.retry_pending.assert_not_called()


def test_recovery_start_and_registration_are_atomic(monkeypatch) -> None:
    """close 不得在恢复线程登记后、真正 start 前把它误判为已收敛。"""
    executor = RecoveryExecutor()
    start_entered = threading.Event()
    allow_start = threading.Event()
    close_results: list[bool] = []
    original_start = threading.Thread.start

    def delayed_recovery_start(thread: threading.Thread) -> None:
        """只暂停恢复线程的 start，稳定放大登记与启动之间的竞态窗口。"""
        if thread.name.startswith("MoviePilot-MonitorRecovery-"):
            start_entered.set()
            assert allow_start.wait(timeout=1)
        original_start(thread)

    monkeypatch.setattr(threading.Thread, "start", delayed_recovery_start)
    runner = threading.Thread(
        target=lambda: executor.run({"atomic": lambda: None}, timeout=1),
        name="recovery-submit-test",
    )
    runner.start()
    assert start_entered.wait(timeout=1)

    closer = threading.Thread(
        target=lambda: close_results.append(
            executor.close(deadline=time.monotonic() + 1)
        ),
        name="recovery-close-test",
    )
    closer.start()
    closer.join(timeout=0.05)
    assert closer.is_alive(), "close 越过了尚未完成 start 的 owner 临界区"

    allow_start.set()
    runner.join(timeout=1)
    closer.join(timeout=1)

    assert not runner.is_alive()
    assert not closer.is_alive()
    assert close_results == [True]
    assert executor.running_threads() == ()


def test_close_tracks_compensation_and_blocks_post_seal_dispatch(tmp_path) -> None:
    """补偿扫描解冻后不得在旧 lifespan 中继续把文件送入整理链。"""
    monitor = _build_monitor()
    entered = threading.Event()
    release = threading.Event()
    candidate = tmp_path / "late.mkv"

    def blocking_collect(_mon_path: Path):
        """模拟补偿扫描阻塞在目录遍历。"""
        entered.set()
        release.wait()
        return [(candidate, time.time(), 1)]

    monitor._Monitor__collect_compensation_files = blocking_collect
    monitor._Monitor__start_compensation(mon_path=tmp_path, since=time.time())
    assert entered.wait(1)

    try:
        assert monitor.close(timeout=0.02) is False
        assert len(monitor._compensation_threads) == 1
        monitor._dispatcher.handle_file.assert_not_called()
    finally:
        release.set()
    assert monitor.close(timeout=1) is True
    assert monitor._compensation_threads == {}
    monitor._dispatcher.handle_file.assert_not_called()


def test_close_tracks_scheduler_shutdown_thread() -> None:
    """scheduler shutdown 阻塞时保留线程和 scheduler，恢复后才能清空句柄。"""
    monitor = _build_monitor()
    entered = threading.Event()
    release = threading.Event()
    scheduler = MagicMock()
    scheduler.running = True

    def blocking_shutdown(*, wait: bool) -> None:
        """模拟等待在途 APScheduler job 的同步 shutdown。"""
        assert wait is True
        entered.set()
        release.wait()

    scheduler.shutdown.side_effect = blocking_shutdown
    monitor._scheduler = scheduler

    try:
        assert monitor.close(timeout=0.02) is False
        assert entered.is_set()
        assert monitor._scheduler is scheduler
        assert monitor._scheduler_shutdown_thread is not None
        assert monitor._scheduler_shutdown_thread.is_alive()
    finally:
        release.set()
    assert monitor.close(timeout=1) is True
    assert monitor._scheduler is None
    assert monitor._scheduler_shutdown_thread is None


@pytest.mark.asyncio
async def test_stop_monitor_cancellation_waits_for_sync_close(monkeypatch) -> None:
    """生命周期取消异步 stop 后，线程池调用仍由任务持有到同步 close 结束。"""
    entered = threading.Event()
    release = threading.Event()

    def blocking_close(timeout: float) -> bool:
        """模拟仍在同步收尾的 Monitor.close。"""
        assert timeout == 1
        entered.set()
        release.wait()
        return True

    monitor = SimpleNamespace(close=blocking_close)
    monkeypatch.setattr(Monitor, "get_existing_instance", lambda: monitor)
    task = asyncio.create_task(stop_monitor(timeout=1))
    while not entered.is_set():
        await asyncio.sleep(0)

    task.cancel()
    await asyncio.sleep(0.01)
    assert task.done() is False
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_init_monitor_explicitly_reopens_existing_lifespan(monkeypatch) -> None:
    """同进程测试中的新 lifespan 必须显式 reopen，再初始化同一个 Monitor。"""
    monitor = SimpleNamespace(
        lifecycle_closed=True,
        reopen=MagicMock(return_value=True),
        init=MagicMock(return_value=True),
    )
    monkeypatch.setattr(Monitor, "get_existing_instance", lambda: monitor)

    init_monitor()

    monitor.reopen.assert_called_once_with(timeout=Monitor.RELOAD_STOP_TIMEOUT)
    monitor.init.assert_called_once_with(timeout=Monitor.RELOAD_STOP_TIMEOUT)


@pytest.mark.asyncio
async def test_constructor_failure_publishes_started_watcher_to_cleanup(
    monkeypatch,
) -> None:
    """Monitor 构造中途失败后，stop-only 入口仍必须找到已启动 watcher owner。"""
    instances = dict(SingletonClass._instances)
    instances.pop(Monitor, None)
    monkeypatch.setattr(SingletonClass, "_instances", instances)
    watcher_started = threading.Event()
    watcher_release = threading.Event()
    watchers: list[threading.Thread] = []

    def failing_init(monitor: Monitor) -> None:
        """模拟 watcher 已启动、后续 scheduler 启动失败的构造过程。"""
        watcher = threading.Thread(
            target=lambda: (watcher_started.set(), watcher_release.wait()),
            name="monitor-partial-construction",
            daemon=True,
        )
        watchers.append(watcher)
        watcher.start()

        def close(timeout: float) -> bool:
            """模拟真实 close 释放并等待半构造实例已经发布的 watcher。"""
            watcher_release.set()
            watcher.join(timeout=timeout)
            return not watcher.is_alive()

        monitor.close = close
        raise RuntimeError("scheduler start failed")

    monkeypatch.setattr(Monitor, "__init__", failing_init)

    with pytest.raises(RuntimeError, match="scheduler start failed"):
        Monitor()
    assert watcher_started.wait(timeout=1)
    retained = Monitor.get_existing_instance()
    assert retained is not None

    assert await stop_monitor(timeout=1) is True
    assert watchers[0].is_alive() is False
