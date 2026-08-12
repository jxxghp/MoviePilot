import os
import time
from unittest.mock import MagicMock

from watchfiles import Change

from app.core.config import settings
from app.monitor import LocalDirectoryWatcher, Monitor
from app.monitor.dispatcher import TransferDispatcher
from app.monitor.recovery import RecoveryExecutor
from app.monitor.syslimits import decide_monitor_mode
from app.utils.system import SystemUtils


def _build_monitor(handle_file: MagicMock = None):
    """
    构造带分发器的测试用 Monitor 骨架。
    :param handle_file: 替换分发器 handle_file 的替身
    :return: (Monitor 骨架, 分发器)
    """
    from threading import Lock
    monitor = object.__new__(Monitor)
    dispatcher = TransferDispatcher(all_exts=[".mkv"], cache={})
    if handle_file is not None:
        dispatcher.handle_file = handle_file
    monitor._dispatcher = dispatcher
    monitor._watchers = []
    monitor._watcher_lock = Lock()
    monitor._alerted_paths = {}
    monitor._restart_marks = {}
    monitor._stable_cycles = {}
    monitor._isolated = {}
    monitor._recovery = RecoveryExecutor()
    return monitor, dispatcher


def _fake_watcher(mon_path, restart_count=0):
    """
    构造存活且未静默失效的监控线程替身。
    :param mon_path: 监控目录
    :param restart_count: 累计自动重启次数
    :return: 监控线程替身
    """
    watcher = MagicMock()
    watcher.watch_path = mon_path
    watcher.is_alive.return_value = True
    watcher.is_stalled.return_value = False
    watcher.restart_count = restart_count
    return watcher


def test_compensation_scan_covers_files_with_old_mtime(tmp_path):
    """
    网盘挂载转存/移动文件会保留原始 mtime，补偿扫描不能因 mtime 旧就漏掉文件，
    但仍应按 mtime 从新到旧优先处理。
    """
    stale = tmp_path / "old.mkv"
    fresh = tmp_path / "new.mkv"
    stale.write_bytes(b"x")
    fresh.write_bytes(b"y")
    now = time.time()
    os.utime(stale, (now - 86400 * 365, now - 86400 * 365))
    os.utime(fresh, (now, now))
    handled = MagicMock(return_value=True)
    monitor, _ = _build_monitor(handle_file=handled)

    monitor._Monitor__compensate_scan(mon_path=tmp_path, since=now - 600)

    handled_paths = [call.kwargs["event_path"] for call in handled.call_args_list]
    assert handled_paths == [fresh, stale]


def test_compensation_scan_limits_single_batch(tmp_path, monkeypatch):
    """候选文件超过上限时只处理最新的一批，避免大目录把整理链与数据库压垮。"""
    monkeypatch.setattr(Monitor, "MAX_COMPENSATION_FILES", 2)
    now = time.time()
    files = []
    for index in range(3):
        target = tmp_path / f"{index}.mkv"
        target.write_bytes(b"x")
        os.utime(target, (now - index * 60, now - index * 60))
        files.append(target)
    handled = MagicMock(return_value=True)
    monitor, _ = _build_monitor(handle_file=handled)

    monitor._Monitor__compensate_scan(mon_path=tmp_path, since=now - 600)

    handled_paths = [call.kwargs["event_path"] for call in handled.call_args_list]
    assert handled_paths == [files[0], files[1]]


def test_watchdog_compensates_after_internal_restart(tmp_path, monkeypatch):
    """
    watcher 内部退避重启期间落地的文件会被新基线快照静默吸收，健康检查发现
    重启计数增长时应补扫一次，且同一次重启不得反复补扫。
    """
    monkeypatch.setattr("app.monitor.monitor.MessageHelper", MagicMock())
    monitor, _ = _build_monitor(handle_file=MagicMock(return_value=True))
    watcher = _fake_watcher(tmp_path, restart_count=1)
    monitor._watchers = [watcher]
    started = []
    setattr(monitor, "_Monitor__start_compensation", lambda **kwargs: started.append(kwargs))

    monitor._Monitor__check_watchers()
    assert len(started) == 1
    assert started[0]["mon_path"] == tmp_path
    # 停摆起点无法精确观测，应保守回溯到最坏情况
    assert 0 < time.time() - started[0]["since"] <= Monitor.RESTART_STALL_LOOKBACK + 5

    monitor._Monitor__check_watchers()
    monitor._Monitor__check_watchers()
    assert len(started) == 1

    watcher.restart_count = 2
    monitor._Monitor__check_watchers()
    assert len(started) == 2


def test_compensation_scan_skips_non_candidate_files(tmp_path):
    """补偿扫描应跳过不属于监控扩展名的文件。"""
    other = tmp_path / "note.txt"
    other.write_bytes(b"x")
    handled = MagicMock(return_value=True)
    monitor, _ = _build_monitor(handle_file=handled)

    monitor._Monitor__compensate_scan(mon_path=tmp_path, since=time.time() - 60)

    handled.assert_not_called()


def test_compensation_skipped_without_activity_record(tmp_path, monkeypatch):
    """没有活动记录就没有可靠的停摆起点，应跳过补偿扫描。"""
    monitor, _ = _build_monitor(handle_file=MagicMock())
    started = []
    monkeypatch.setattr("app.monitor.monitor.Thread",
                        lambda **kwargs: started.append(kwargs) or MagicMock())

    monitor._Monitor__start_compensation(mon_path=tmp_path, since=0)
    assert started == []

    monitor._Monitor__start_compensation(mon_path=tmp_path, since=time.time())
    assert len(started) == 1


def test_unreadable_event_is_queued_instead_of_dropped(tmp_path, monkeypatch):
    """读取文件大小失败的事件应登记待重试，而不是被静默丢弃。"""
    target = tmp_path / "a.mkv"
    target.write_bytes(b"x")
    monitor, dispatcher = _build_monitor(handle_file=MagicMock(return_value=True))
    watcher = LocalDirectoryWatcher(tmp_path, callback=monitor, force_polling=True)
    monkeypatch.setattr(LocalDirectoryWatcher, "_get_file_size", staticmethod(lambda _p: None))

    watcher._dispatch_changes({(Change.added, target.as_posix())})

    assert len(dispatcher._pending_retries) == 1
    entry = next(iter(dispatcher._pending_retries.values()))
    assert entry["file_size"] is None


def test_retry_pending_reresolves_missing_file_size(tmp_path):
    """重试时要重新读取文件大小，再把文件送入整理链。"""
    target = tmp_path / "a.mkv"
    target.write_bytes(b"12345")
    handled = MagicMock(return_value=True)
    _, dispatcher = _build_monitor(handle_file=handled)
    dispatcher.register_unreadable(storage="local", event_path=target)

    dispatcher.retry_pending()

    assert handled.call_args.kwargs["file_size"] == 5


def test_retry_pending_drops_vanished_file(tmp_path):
    """待重试文件已经消失时应放弃登记，不再无谓重试。"""
    target = tmp_path / "gone.mkv"
    handled = MagicMock(return_value=True)
    _, dispatcher = _build_monitor(handle_file=handled)
    dispatcher.register_unreadable(storage="local", event_path=target)

    dispatcher.retry_pending()

    assert dispatcher._pending_retries == {}
    handled.assert_not_called()


def test_transfer_failure_invalidates_dedup_cache(tmp_path, monkeypatch):
    """整理抛异常时去重缓存必须失效，否则 TTL 窗口内的后续事件会被吞掉。"""
    target = tmp_path / "a.mkv"
    target.write_bytes(b"x")
    dispatcher = TransferDispatcher(all_exts=[".mkv"], cache={})
    monkeypatch.setattr(dispatcher, "_should_skip_by_history", lambda **kwargs: False)

    class _FailingChain:
        """整理时固定抛异常的整理链替身。"""

        @staticmethod
        def do_transfer(**kwargs):
            """模拟整理过程抛出异常。"""
            raise RuntimeError("整理失败")

    monkeypatch.setattr("app.monitor.dispatcher.TransferChain", _FailingChain)

    assert dispatcher.handle_file(storage="local", event_path=target, file_size=1) is False
    assert dispatcher._cache == {}


def test_delayed_rescan_picks_up_late_visible_files(tmp_path, monkeypatch):
    """新增目录首次展开时不可见的文件，应由延迟重扫补回。"""
    monkeypatch.setattr(LocalDirectoryWatcher, "DIRECTORY_RESCAN_DELAYS", (0,))
    new_dir = tmp_path / "season"
    new_dir.mkdir()
    first = new_dir / "E03.mkv"
    first.write_bytes(b"x")
    recorder = MagicMock()
    watcher = LocalDirectoryWatcher(tmp_path, callback=recorder, force_polling=True)

    watcher._handle_changes({(Change.added, new_dir.as_posix())})
    first_paths = {call.kwargs["event_path"] for call in recorder.event_handler.call_args_list}
    assert first.as_posix() in first_paths

    # 目录内容延迟可见：第二个文件此时才出现，不会再产生任何 watchfiles 事件
    late = new_dir / "E04.mkv"
    late.write_bytes(b"y")
    recorder.event_handler.reset_mock()

    watcher._process_pending_rescans()

    rescan_paths = {call.kwargs["event_path"] for call in recorder.event_handler.call_args_list}
    assert rescan_paths == {late.as_posix()}


def test_network_filesystem_forces_polling_by_default(tmp_path, monkeypatch):
    """默认情况下网络文件系统仍应强制兼容模式。"""
    monkeypatch.setattr(SystemUtils, "is_network_filesystem", staticmethod(lambda _d: True))
    monkeypatch.setattr(settings, "MONITOR_NETWORK_FAST_MODE", False)

    use_polling, _, _, _ = decide_monitor_mode(tmp_path, "fast")

    assert use_polling is True


def test_network_filesystem_can_opt_into_fast_mode(tmp_path, monkeypatch):
    """用户确认挂载支持 inotify 后应允许快速模式。"""
    monkeypatch.setattr(SystemUtils, "is_network_filesystem", staticmethod(lambda _d: True))
    monkeypatch.setattr(settings, "MONITOR_NETWORK_FAST_MODE", True)

    use_polling, _, _, _ = decide_monitor_mode(tmp_path, "fast")

    assert use_polling is False


def test_compatibility_mode_still_wins_over_fast_mode_override(tmp_path, monkeypatch):
    """用户显式配置兼容模式时，快速模式开关不得反向覆盖。"""
    monkeypatch.setattr(SystemUtils, "is_network_filesystem", staticmethod(lambda _d: True))
    monkeypatch.setattr(settings, "MONITOR_NETWORK_FAST_MODE", True)

    use_polling, _, _, _ = decide_monitor_mode(tmp_path, "compatibility")

    assert use_polling is True
