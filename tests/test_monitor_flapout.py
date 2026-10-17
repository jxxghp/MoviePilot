"""
分发瞬间 flap-out 事件不丢弃测试(§5.2③)。

FUSE 挂载抖动时,事件到达派发环节的瞬间文件可能恰好从视图中消失
(exists() 为 False)。原实现在路径"确认不存在"时静默丢弃事件,
而 flap-out 与真删除在这一瞬间无法区分,丢弃就是永久漏件。
正确行为:一律登记待重试,由重试队列在 60s 周期里区分
"恢复可见(继续整理)"与"确实已删除(自动放弃)"。
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from watchfiles import Change

from app.monitor.watcher import LocalDirectoryWatcher


@pytest.fixture
def watcher_context(tmp_path: Path) -> SimpleNamespace:
    """
    在 pytest 临时目录中创建监听器与回调桩，不启动真实监控线程。

    :return: 包含监控目录 mon_path、回调 callback 和监听器 watcher 的测试上下文
    """
    callback = Mock()
    return SimpleNamespace(
        mon_path=tmp_path,
        callback=callback,
        watcher=LocalDirectoryWatcher(mon_path=tmp_path, callback=callback),
    )


def test_vanished_file_event_registers_retry(watcher_context) -> None:
    """
    事件路径已不可见(flap-out 或删除)时必须登记重试,而非静默丢弃。
    """
    vanished = watcher_context.mon_path / "vanished.mkv"
    watcher_context.watcher._dispatch_changes({(Change.added, vanished.as_posix())})
    watcher_context.callback.event_unreadable.assert_called_once_with(event_path=vanished)
    watcher_context.callback.event_handler.assert_not_called()


def test_existing_file_event_still_dispatched(watcher_context) -> None:
    """
    正常存在的文件事件照常派发,不受重试登记逻辑影响。
    """
    existing = watcher_context.mon_path / "existing.mkv"
    existing.write_bytes(b"data")
    watcher_context.watcher._dispatch_changes({(Change.added, existing.as_posix())})
    watcher_context.callback.event_handler.assert_called_once()
    watcher_context.callback.event_unreadable.assert_not_called()
