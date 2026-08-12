"""
目录监控延迟重扫机制的边界缺陷回归测试。

覆盖点：
- 同目录重复登记去重（4.1）
- 整体扫描失败不燃烧轮次 + 失败次数上限（4.2）
- 目录删除后条目出队，不计入失败重试（4.2）
- MONITOR_RESCAN_DELAYS 配置解析（合法/非法回退）（4.3）
- 目录树整体移入时只登记顶层目录、祖先已登记时跳过子孙目录（4.4）
- 待重扫队列溢出时日志升级为 warn（4.4）
"""
import shutil
from pathlib import Path
from unittest.mock import MagicMock

from watchfiles import Change

from app.core.config import settings
from app.monitor.watcher import LocalDirectoryWatcher


def _build_watcher(tmp_path, force_polling=True):
    """
    构造测试用目录监控。
    :param tmp_path: 监控目录
    :param force_polling: 是否强制轮询
    :return: 目录监控
    """
    return LocalDirectoryWatcher(tmp_path, callback=MagicMock(), force_polling=force_polling)


# ==================== 4.1 同目录重复登记去重 ====================

def test_schedule_rescan_dedups_same_directory(tmp_path):
    """
    readdir 闪断可能让同一目录产生两次 added 事件，重复调用 _schedule_rescan
    只应保留一条待重扫记录，不应重复登记。
    """
    directory = tmp_path / "season"
    directory.mkdir()
    watcher = _build_watcher(tmp_path)

    watcher._schedule_rescan(directory, seen=set())
    watcher._schedule_rescan(directory, seen={"already-seen"})

    assert len(watcher._pending_rescans) == 1
    # 保留原条目即可，不需要用新事件的 seen 覆盖
    assert watcher._pending_rescans[0]["seen"] == set()


def test_expand_added_directories_dedups_repeated_added_event_across_batches(tmp_path):
    """
    同一目录在不同批次的 changes 中各出现一次 added 事件时（模拟 readdir 闪断），
    整体展开流程也不应重复登记重扫。
    """
    directory = tmp_path / "season"
    directory.mkdir()
    watcher = _build_watcher(tmp_path)

    watcher._expand_added_directories({(Change.added, directory.as_posix())})
    watcher._expand_added_directories({(Change.added, directory.as_posix())})

    assert len(watcher._pending_rescans) == 1


# ==================== 4.2 扫描失败不燃烧轮次 + 失败上限 + 目录删除终态 ====================

def test_collect_directory_files_reports_missing_directory(tmp_path):
    """
    目录已不存在时应识别为终态（missing=True），而不是当作扫描失败。
    """
    missing = tmp_path / "gone"
    watcher = _build_watcher(tmp_path)

    collected, is_missing, scan_failed = watcher._collect_directory_files(missing, exclude=set())

    assert collected == set()
    assert is_missing is True
    assert scan_failed is False


def test_collect_directory_files_reports_scan_failure(tmp_path, monkeypatch):
    """
    顶层 rglob 抛 OSError 应识别为整体扫描失败（scan_failed=True），
    与目录已删除的终态区分开。
    """
    directory = tmp_path / "season"
    directory.mkdir()

    def failing_rglob(self, pattern):
        raise OSError("FUSE 抖动")

    monkeypatch.setattr(Path, "rglob", failing_rglob)
    watcher = _build_watcher(tmp_path)

    collected, is_missing, scan_failed = watcher._collect_directory_files(directory, exclude=set())

    assert collected == set()
    assert is_missing is False
    assert scan_failed is True


def test_process_pending_rescans_does_not_burn_round_on_scan_failure(tmp_path, monkeypatch):
    """
    整体扫描失败时不应消耗重扫轮次，条目应保持在原轮次继续重试，
    并累计一次失败计数。
    """
    monkeypatch.setattr(LocalDirectoryWatcher, "DIRECTORY_RESCAN_DELAYS", (0, 100))
    directory = tmp_path / "season"
    directory.mkdir()
    watcher = _build_watcher(tmp_path)
    watcher._schedule_rescan(directory, seen=set())

    def failing_rglob(self, pattern):
        raise OSError("FUSE 抖动")

    monkeypatch.setattr(Path, "rglob", failing_rglob)

    watcher._process_pending_rescans()

    assert len(watcher._pending_rescans) == 1
    item = watcher._pending_rescans[0]
    assert item["round"] == 0
    assert item["failures"] == 1


def test_process_pending_rescans_drops_item_after_max_failures(tmp_path, monkeypatch):
    """
    连续扫描失败达到 MAX_RESCAN_FAILURES 上限后应放弃重扫并 warn，
    避免目录长期不可访问时无限重试。
    """
    monkeypatch.setattr(LocalDirectoryWatcher, "DIRECTORY_RESCAN_DELAYS", (0,))
    monkeypatch.setattr(LocalDirectoryWatcher, "MAX_RESCAN_FAILURES", 2)
    logger_warn = MagicMock()
    monkeypatch.setattr("app.monitor.watcher.logger.warn", logger_warn)
    directory = tmp_path / "season"
    directory.mkdir()
    watcher = _build_watcher(tmp_path)
    watcher._schedule_rescan(directory, seen=set())

    def failing_rglob(self, pattern):
        raise OSError("FUSE 抖动")

    monkeypatch.setattr(Path, "rglob", failing_rglob)

    watcher._process_pending_rescans()
    assert len(watcher._pending_rescans) == 1

    watcher._process_pending_rescans()

    assert watcher._pending_rescans == []
    logger_warn.assert_called_once()


def test_process_pending_rescans_resets_failure_count_after_success(tmp_path, monkeypatch):
    """
    扫描恢复成功后应重置失败计数，不应带着历史失败次数继续累积。
    """
    monkeypatch.setattr(LocalDirectoryWatcher, "DIRECTORY_RESCAN_DELAYS", (0, 100))
    directory = tmp_path / "season"
    directory.mkdir()
    watcher = _build_watcher(tmp_path)
    watcher._schedule_rescan(directory, seen=set())
    watcher._pending_rescans[0]["failures"] = 3

    watcher._process_pending_rescans()

    assert len(watcher._pending_rescans) == 1
    assert watcher._pending_rescans[0]["failures"] == 0


def test_process_pending_rescans_drops_deleted_directory_without_failure(tmp_path, monkeypatch):
    """
    目录已被删除是终态，应直接出队，不计入失败重试次数。
    """
    monkeypatch.setattr(LocalDirectoryWatcher, "DIRECTORY_RESCAN_DELAYS", (0,))
    directory = tmp_path / "season"
    directory.mkdir()
    watcher = _build_watcher(tmp_path)
    watcher._schedule_rescan(directory, seen=set())
    shutil.rmtree(directory)

    watcher._process_pending_rescans()

    assert watcher._pending_rescans == []


# ==================== 4.3 重扫窗口可配置 ====================

def test_parse_rescan_delays_accepts_valid_string():
    """
    合法的逗号分隔正整数字符串应正确解析为元组。
    """
    assert LocalDirectoryWatcher._parse_rescan_delays("30,120,600,1800") == (30, 120, 600, 1800)
    assert LocalDirectoryWatcher._parse_rescan_delays(" 5 , 10 ") == (5, 10)


def test_parse_rescan_delays_falls_back_on_empty():
    """
    空字符串/None 应回退到默认值。
    """
    assert LocalDirectoryWatcher._parse_rescan_delays("") == LocalDirectoryWatcher.DEFAULT_RESCAN_DELAYS
    assert LocalDirectoryWatcher._parse_rescan_delays(None) == LocalDirectoryWatcher.DEFAULT_RESCAN_DELAYS


def test_parse_rescan_delays_falls_back_on_invalid_format(monkeypatch):
    """
    非法格式（无法转换为整数）应回退默认值并记录 warn 日志。
    """
    logger_warn = MagicMock()
    monkeypatch.setattr("app.monitor.watcher.logger.warn", logger_warn)

    assert LocalDirectoryWatcher._parse_rescan_delays("abc,def") == LocalDirectoryWatcher.DEFAULT_RESCAN_DELAYS
    logger_warn.assert_called_once()


def test_parse_rescan_delays_rejects_non_positive_values(monkeypatch):
    """
    包含非正整数（0 或负数）视为非法配置，应回退默认值并记录 warn 日志。
    """
    logger_warn = MagicMock()
    monkeypatch.setattr("app.monitor.watcher.logger.warn", logger_warn)

    assert LocalDirectoryWatcher._parse_rescan_delays("30,-5") == LocalDirectoryWatcher.DEFAULT_RESCAN_DELAYS
    logger_warn.assert_called_once()


def test_directory_rescan_delays_property_reads_settings(tmp_path, monkeypatch):
    """
    DIRECTORY_RESCAN_DELAYS 应实时反映 MONITOR_RESCAN_DELAYS 配置。
    """
    monkeypatch.setattr(settings, "MONITOR_RESCAN_DELAYS", "5,10")
    watcher = _build_watcher(tmp_path)

    assert watcher.DIRECTORY_RESCAN_DELAYS == (5, 10)


def test_directory_rescan_delays_property_falls_back_on_invalid_settings(tmp_path, monkeypatch):
    """
    配置非法时属性访问应回退默认值，而不是抛异常影响监控主流程。
    """
    logger_warn = MagicMock()
    monkeypatch.setattr("app.monitor.watcher.logger.warn", logger_warn)
    monkeypatch.setattr(settings, "MONITOR_RESCAN_DELAYS", "not-a-number")
    watcher = _build_watcher(tmp_path)

    assert watcher.DIRECTORY_RESCAN_DELAYS == LocalDirectoryWatcher.DEFAULT_RESCAN_DELAYS


# ==================== 4.4 目录树移入只登记顶层 + 溢出 warn ====================

def test_expand_added_directories_only_schedules_top_level(tmp_path):
    """
    大目录树整体移入时，changes 里每一层子目录都会各自产生一次 added 事件，
    但只有顶层目录需要登记重扫：子目录内容已被顶层目录的 rglob 覆盖。
    """
    top = tmp_path / "task"
    nested = top / "season1"
    nested2 = nested / "sub"
    nested2.mkdir(parents=True)
    (top / "movie.mkv").write_bytes(b"x")
    watcher = _build_watcher(tmp_path)

    watcher._expand_added_directories({
        (Change.added, top.as_posix()),
        (Change.added, nested.as_posix()),
        (Change.added, nested2.as_posix()),
    })

    assert len(watcher._pending_rescans) == 1
    assert watcher._pending_rescans[0]["path"] == top


def test_schedule_rescan_skips_descendant_of_pending_ancestor(tmp_path):
    """
    某祖先目录已经在待重扫队列中时，其子孙目录不应再单独登记——
    祖先条目的 rglob 会递归覆盖到子孙目录的新文件。
    """
    parent = tmp_path / "task"
    child = parent / "season"
    child.mkdir(parents=True)
    watcher = _build_watcher(tmp_path)
    watcher._schedule_rescan(parent, seen=set())

    watcher._schedule_rescan(child, seen=set())

    assert len(watcher._pending_rescans) == 1
    assert watcher._pending_rescans[0]["path"] == parent


def test_is_descendant_of_any_does_not_match_sibling_with_prefix_name(tmp_path):
    """
    路径前缀相同但并非真实父子关系的兄弟目录（如 task / task2）不应被
    误判为祖先命中，避免字符串前缀匹配导致的误跳过。
    """
    parent = tmp_path / "task"
    sibling = tmp_path / "task2"

    assert LocalDirectoryWatcher._is_descendant_of_any(sibling, {parent}) is False


def test_schedule_rescan_overflow_logs_warning(tmp_path, monkeypatch):
    """
    待重扫队列已满时应放弃登记，且日志级别应为 warn（原实现仅 debug，
    可能导致文件永久漏扫却难以被发现）。
    """
    monkeypatch.setattr(LocalDirectoryWatcher, "MAX_PENDING_RESCANS", 1)
    logger_warn = MagicMock()
    monkeypatch.setattr("app.monitor.watcher.logger.warn", logger_warn)
    watcher = _build_watcher(tmp_path)
    first_dir = tmp_path / "a"
    second_dir = tmp_path / "b"
    first_dir.mkdir()
    second_dir.mkdir()

    watcher._schedule_rescan(first_dir, seen=set())
    watcher._schedule_rescan(second_dir, seen=set())

    assert len(watcher._pending_rescans) == 1
    assert watcher._pending_rescans[0]["path"] == first_dir
    logger_warn.assert_called_once()
