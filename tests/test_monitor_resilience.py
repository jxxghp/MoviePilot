from pathlib import Path
from unittest.mock import MagicMock

from app.monitor import LocalDirectoryWatcher, Monitor


def _build_watcher(tmp_path, force_polling):
    """
    构造测试用目录监控。
    :param tmp_path: 监控目录
    :param force_polling: 是否强制轮询
    :return: 目录监控
    """
    return LocalDirectoryWatcher(tmp_path, callback=MagicMock(), force_polling=force_polling)


def test_run_retries_with_backoff_in_compatibility_mode(tmp_path, monkeypatch):
    """
    兼容模式下监控循环抛异常后应退避重启，而不是直接结束线程。
    """
    monkeypatch.setattr(LocalDirectoryWatcher, "RESTART_BACKOFF", (0,))
    watcher = _build_watcher(tmp_path, force_polling=True)
    calls = []

    def fake_run_watch(force_polling):
        """
        模拟底层监控循环持续抛出 FUSE 错误。
        """
        calls.append(force_polling)
        if len(calls) >= 3:
            watcher.stop()
        raise OSError(131, "State not recoverable")

    monkeypatch.setattr(watcher, "_run_watch", fake_run_watch)

    watcher._run()

    assert calls == [True, True, True]
    assert watcher.restart_count == 2


def test_run_falls_back_to_polling_before_backoff(tmp_path, monkeypatch):
    """
    快速模式失败应先降级为兼容模式重试，且降级不计入退避重启次数。
    """
    monkeypatch.setattr(LocalDirectoryWatcher, "RESTART_BACKOFF", (0,))
    watcher = _build_watcher(tmp_path, force_polling=None)
    calls = []

    def fake_run_watch(force_polling):
        """
        模拟快速模式与兼容模式先后失败。
        """
        calls.append(force_polling)
        if len(calls) >= 2:
            watcher.stop()
        raise OSError("inotify watch limit reached")

    monkeypatch.setattr(watcher, "_run_watch", fake_run_watch)

    watcher._run()

    assert calls == [None, True]
    assert watcher.restart_count == 0


def test_run_returns_when_stop_requested(tmp_path, monkeypatch):
    """
    收到停止信号后监控循环正常返回，不应触发重启。
    """
    watcher = _build_watcher(tmp_path, force_polling=True)
    calls = []

    def fake_run_watch(force_polling):
        """
        模拟收到停止信号后正常退出的监控循环。
        """
        calls.append(force_polling)

    monkeypatch.setattr(watcher, "_run_watch", fake_run_watch)

    watcher._run()

    assert calls == [True]
    assert watcher.restart_count == 0


def test_is_stalled_detects_silent_failure(tmp_path):
    """
    监控线程存活但长时间无活动时应判定为静默失效。
    """
    watcher = _build_watcher(tmp_path, force_polling=True)

    # 线程未启动时不做判定
    assert watcher.is_stalled() is False

    thread = MagicMock()
    thread.is_alive.return_value = True
    watcher._thread = thread
    watcher._mark_activity()
    assert watcher.is_stalled() is False

    watcher._last_activity -= LocalDirectoryWatcher.STALL_TIMEOUT + 1
    assert watcher.is_stalled() is True


def test_is_stalled_ignores_stopped_watcher(tmp_path):
    """
    已请求停止的监控不应再被判定为静默失效。
    """
    watcher = _build_watcher(tmp_path, force_polling=True)
    thread = MagicMock()
    thread.is_alive.return_value = True
    watcher._thread = thread
    watcher._mark_activity()
    watcher._last_activity -= LocalDirectoryWatcher.STALL_TIMEOUT + 1

    watcher.stop()

    assert watcher.is_stalled() is False


def _build_monitor(monkeypatch, put_recorder):
    """
    构造测试用 Monitor 骨架，绕过单例初始化。
    :param monkeypatch: pytest monkeypatch
    :param put_recorder: 消息推送记录器
    :return: Monitor 骨架
    """
    from threading import Lock
    monkeypatch.setattr("app.monitor.monitor.MessageHelper", MagicMock(return_value=put_recorder))
    monitor = object.__new__(Monitor)
    monitor._watchers = []
    monitor._watcher_lock = Lock()
    monitor._pending_locals = []
    monitor._alerted_paths = set()
    monitor._restart_marks = {}
    monitor._stable_cycles = {}
    return monitor


def _fake_watcher(mon_path, alive=True, stalled=False, restart_count=0):
    """
    构造测试用监控线程替身。
    :param mon_path: 监控目录
    :param alive: 线程是否存活
    :param stalled: 是否静默失效
    :param restart_count: 自动重启次数
    :return: 监控线程替身
    """
    watcher = MagicMock()
    watcher.watch_path = mon_path
    watcher.is_alive.return_value = alive
    watcher.is_stalled.return_value = stalled
    watcher.restart_count = restart_count
    return watcher


def test_watchdog_rebuilds_dead_watcher(tmp_path, monkeypatch):
    """
    监控线程退出后健康检查应重建线程并告警。
    """
    put_recorder = MagicMock()
    monitor = _build_monitor(monkeypatch, put_recorder)
    watcher = _fake_watcher(tmp_path, alive=False)
    monitor._watchers = [watcher]
    rebuild = MagicMock()
    setattr(monitor, "_Monitor__rebuild_watcher", rebuild)

    monitor._Monitor__check_watchers()

    rebuild.assert_called_once_with(watcher)
    put_recorder.put.assert_called_once()


def test_watchdog_rebuilds_stalled_watcher(tmp_path, monkeypatch):
    """
    静默失效的监控线程也应被健康检查重建。
    """
    put_recorder = MagicMock()
    monitor = _build_monitor(monkeypatch, put_recorder)
    watcher = _fake_watcher(tmp_path, alive=True, stalled=True)
    monitor._watchers = [watcher]
    rebuild = MagicMock()
    setattr(monitor, "_Monitor__rebuild_watcher", rebuild)

    monitor._Monitor__check_watchers()

    rebuild.assert_called_once_with(watcher)


def test_watchdog_alerts_on_restart_and_recovers_after_stable_window(tmp_path, monkeypatch):
    """
    自动重启应触发一次告警，恢复消息需等满稳定窗口，避免来回刷屏。
    """
    put_recorder = MagicMock()
    monitor = _build_monitor(monkeypatch, put_recorder)
    watcher = _fake_watcher(tmp_path, alive=True, stalled=False, restart_count=1)
    monitor._watchers = [watcher]

    monitor._Monitor__check_watchers()
    assert str(tmp_path) in monitor._alerted_paths
    assert put_recorder.put.call_count == 1

    for _ in range(Monitor.RECOVERY_STABLE_CYCLES - 1):
        monitor._Monitor__check_watchers()
    assert str(tmp_path) in monitor._alerted_paths

    monitor._Monitor__check_watchers()
    assert str(tmp_path) not in monitor._alerted_paths
    assert put_recorder.put.call_count == 2


def test_retry_pending_locals_backs_off(tmp_path, monkeypatch):
    """
    启动失败的监控重试应按失败次数退避，避免持续故障时刷屏。
    """
    put_recorder = MagicMock()
    monitor = _build_monitor(monkeypatch, put_recorder)
    monitor._pending_locals = [{"mon_path": tmp_path, "monitor_mode": "compatibility"}]
    start = MagicMock(return_value=False)
    setattr(monitor, "_Monitor__start_local_monitor", start)

    for _ in range(6):
        monitor._Monitor__retry_pending_locals()

    assert start.call_count == 3


def test_dispatcher_retries_after_history_query_failure(monkeypatch):
    """
    整理历史查询失败应登记待重试，重试成功后进入整理链并清除登记。
    """
    from app.monitor.dispatcher import TransferDispatcher
    dispatcher = TransferDispatcher(all_exts=[".mkv"], cache={})
    event_path = Path("/downloads/movie.mkv")
    history = MagicMock(side_effect=[None, False])
    monkeypatch.setattr(dispatcher, "_has_transfer_history", history)
    transfer_chain_instance = MagicMock()
    monkeypatch.setattr("app.monitor.dispatcher.TransferChain",
                        MagicMock(return_value=transfer_chain_instance))

    # 首次查询失败：不整理，登记待重试
    assert dispatcher.handle_file(storage="local", event_path=event_path, file_size=1) is False
    assert len(dispatcher._pending_retries) == 1
    transfer_chain_instance.do_transfer.assert_not_called()

    # 模拟 TTL 缓存过期后由健康检查驱动重试
    dispatcher._cache.clear()
    dispatcher.retry_pending()

    transfer_chain_instance.do_transfer.assert_called_once()
    assert dispatcher._pending_retries == {}


def test_dispatcher_drops_pending_after_max_attempts(monkeypatch):
    """
    历史查询持续失败达到上限后应放弃重试，避免队列无限累积。
    """
    from app.monitor.dispatcher import TransferDispatcher
    dispatcher = TransferDispatcher(all_exts=[".mkv"], cache={})
    event_path = Path("/downloads/movie.mkv")
    monkeypatch.setattr(dispatcher, "_has_transfer_history", MagicMock(return_value=None))

    dispatcher.handle_file(storage="local", event_path=event_path, file_size=1)
    key = f"local:{event_path.as_posix()}"
    assert key in dispatcher._pending_retries
    dispatcher._pending_retries[key]["attempts"] = TransferDispatcher.MAX_RETRY_ATTEMPTS - 1

    dispatcher._cache.clear()
    dispatcher.retry_pending()

    assert dispatcher._pending_retries == {}
