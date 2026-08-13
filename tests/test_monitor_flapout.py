"""
分发瞬间 flap-out 事件不丢弃测试(§5.2③)。

FUSE 挂载抖动时,事件到达派发环节的瞬间文件可能恰好从视图中消失
(exists() 为 False)。原实现在路径"确认不存在"时静默丢弃事件,
而 flap-out 与真删除在这一瞬间无法区分,丢弃就是永久漏件。
正确行为:一律登记待重试,由重试队列在 60s 周期里区分
"恢复可见(继续整理)"与"确实已删除(自动放弃)"。
"""
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

from watchfiles import Change

from app.monitor.watcher import LocalDirectoryWatcher


class FlapOutDispatchTest(unittest.TestCase):

    def setUp(self):
        self._tmpdir = TemporaryDirectory()
        self.mon_path = Path(self._tmpdir.name)
        self.callback = Mock()
        self.watcher = LocalDirectoryWatcher(
            mon_path=self.mon_path, callback=self.callback)

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_vanished_file_event_registers_retry(self):
        """事件路径已不可见(flap-out 或删除)时必须登记重试,而非静默丢弃。"""
        vanished = self.mon_path / "vanished.mkv"
        self.watcher._dispatch_changes({(Change.added, vanished.as_posix())})
        self.callback.event_unreadable.assert_called_once_with(event_path=vanished)
        self.callback.event_handler.assert_not_called()

    def test_existing_file_event_still_dispatched(self):
        """正常存在的文件事件照常派发,不受重试登记逻辑影响。"""
        existing = self.mon_path / "existing.mkv"
        existing.write_bytes(b"data")
        self.watcher._dispatch_changes({(Change.added, existing.as_posix())})
        self.callback.event_handler.assert_called_once()
        self.callback.event_unreadable.assert_not_called()


if __name__ == "__main__":
    unittest.main()
