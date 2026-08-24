import threading
import time
from unittest.mock import MagicMock

import pytest

from app.runtime.log import (
    LogEntry,
    LoggerManager,
    NonBlockingFileHandler,
    log_settings,
)


def test_non_blocking_file_handler_shutdown_wakes_writer_and_closes_handlers(tmp_path):
    """日志关闭应立即唤醒空闲写线程，并关闭所有已打开的文件处理器"""
    original_instance = NonBlockingFileHandler._instance
    NonBlockingFileHandler._instance = None
    handler = NonBlockingFileHandler()
    handler._rotating_handlers = {}
    log_handler = handler._get_rotating_handler(tmp_path / "shutdown.log")

    try:
        started_at = time.monotonic()
        handler.shutdown()
        elapsed = time.monotonic() - started_at

        assert elapsed < 1
        assert not handler._write_thread.is_alive()
        assert log_handler.stream is None
        assert handler._write_non_blocking(
            LogEntry("info", "late-message", tmp_path / "shutdown.log")
        ) is False
        assert handler._write_queue.empty()
    finally:
        if handler._write_thread.is_alive():
            handler._running = False
            handler._write_thread.join(timeout=5)
        if log_handler.stream is not None:
            log_handler.close()
        NonBlockingFileHandler._instance = original_instance


def test_non_blocking_file_handler_shutdown_drains_queued_batches(monkeypatch, tmp_path):
    """停止标记之前已进入队列的日志应跨批次全部写完"""
    original_instance = NonBlockingFileHandler._instance
    NonBlockingFileHandler._instance = None
    monkeypatch.setattr(log_settings, "BATCH_WRITE_SIZE", 2)
    handler = NonBlockingFileHandler()
    handler._rotating_handlers = {}
    written = []
    monkeypatch.setattr(
        handler,
        "_write_batch",
        lambda batch: written.extend(entry.message for entry in batch),
    )

    try:
        for index in range(5):
            handler._write_non_blocking(
                LogEntry("info", f"message-{index}", tmp_path / "drain.log")
            )

        handler.shutdown()

        assert written == [f"message-{index}" for index in range(5)]
        assert not handler._write_thread.is_alive()
    finally:
        if handler._write_thread.is_alive():
            handler._running = False
            handler._write_queue.put(handler._stop_sentinel)
            handler._write_thread.join(timeout=5)
        NonBlockingFileHandler._instance = original_instance


def test_non_blocking_file_handler_creates_one_handler_for_concurrent_first_write(monkeypatch, tmp_path):
    """同一路径首次并发写入时只创建并关闭一个文件处理器"""
    original_instance = NonBlockingFileHandler._instance
    NonBlockingFileHandler._instance = None
    handler = NonBlockingFileHandler()
    handler._rotating_handlers = {}
    first_created = threading.Event()
    second_started = threading.Event()
    release_first = threading.Event()
    created_handlers = []
    results = []

    class ProbeHandler:
        def __init__(self, **kwargs):
            self.closed = False
            created_handlers.append(self)
            if len(created_handlers) == 1:
                first_created.set()
                release_first.wait(timeout=2)

        @staticmethod
        def setFormatter(formatter):
            pass

        @staticmethod
        def flush():
            pass

        def close(self):
            self.closed = True

    monkeypatch.setattr("app.runtime.log.RotatingFileHandler", ProbeHandler)
    file_path = tmp_path / "concurrent.log"

    def get_handler(started=None):
        if started:
            started.set()
        results.append(handler._get_rotating_handler(file_path))

    first = threading.Thread(target=get_handler)
    second = threading.Thread(target=get_handler, args=(second_started,))
    try:
        first.start()
        assert first_created.wait(timeout=1)
        second.start()
        assert second_started.wait(timeout=1)
        time.sleep(0.05)
        release_first.set()
        first.join(timeout=2)
        second.join(timeout=2)

        assert len(created_handlers) == 1
        assert results[0] is results[1]

        handler.shutdown()
        assert created_handlers[0].closed is True
    finally:
        release_first.set()
        first.join(timeout=2)
        second.join(timeout=2)
        handler.shutdown()
        NonBlockingFileHandler._instance = original_instance


def test_non_blocking_file_handler_uses_handler_lock(monkeypatch, tmp_path):
    """日志写入通过 Handler 入口串行化 emit 与 rollover"""
    original_instance = NonBlockingFileHandler._instance
    NonBlockingFileHandler._instance = None
    handler = NonBlockingFileHandler()
    handler._rotating_handlers = {}
    log_handler = MagicMock()
    monkeypatch.setattr(handler, "_get_rotating_handler", MagicMock(return_value=log_handler))

    try:
        handler._write_sync(LogEntry("info", "message", tmp_path / "locked.log"))

        log_handler.handle.assert_called_once()
        log_handler.emit.assert_not_called()
    finally:
        handler.shutdown()
        NonBlockingFileHandler._instance = original_instance


def test_non_blocking_file_handler_shutdown_is_bounded_and_retryable(
    monkeypatch,
    tmp_path,
):
    """批量写入阻塞时关闭必须有限返回，并保留同一 writer 供重试。"""
    original_instance = NonBlockingFileHandler._instance
    NonBlockingFileHandler._instance = None
    monkeypatch.setattr(log_settings, "WRITE_TIMEOUT", 0.01)
    handler = NonBlockingFileHandler()
    entered = threading.Event()
    release = threading.Event()

    def block_batch(_batch):
        """模拟文件系统写入永久占用批量 writer。"""
        entered.set()
        release.wait()

    monkeypatch.setattr(handler, "_write_batch", block_batch)
    try:
        assert handler._write_non_blocking(
            LogEntry("info", "blocked", tmp_path / "blocked.log")
        ) is True
        assert entered.wait(timeout=1)

        started_at = time.monotonic()
        assert handler.shutdown(timeout=0.01) is False
        assert time.monotonic() - started_at < 1
        assert handler._write_thread.is_alive()
        assert handler._write_non_blocking(
            LogEntry("info", "late", tmp_path / "blocked.log")
        ) is False
        release.set()
        assert handler.shutdown(timeout=1) is True
        assert not handler._write_thread.is_alive()
        assert handler.shutdown(timeout=1) is True
    finally:
        release.set()
        handler.shutdown(timeout=1)
        NonBlockingFileHandler._instance = original_instance


def test_non_blocking_file_handler_does_not_bypass_full_queue(
    monkeypatch,
    tmp_path,
):
    """队列达到显式容量后不得再通过无界线程池形成第二条写入路径。"""
    original_instance = NonBlockingFileHandler._instance
    NonBlockingFileHandler._instance = None
    monkeypatch.setattr(log_settings, "ASYNC_FILE_QUEUE_SIZE", 1)
    monkeypatch.setattr(log_settings, "BATCH_WRITE_SIZE", 1)
    handler = NonBlockingFileHandler()
    entered = threading.Event()
    release = threading.Event()
    written: list[str] = []

    def block_batch(batch):
        """占住唯一 writer，使后续日志稳定留在有界队列中。"""
        written.extend(entry.message for entry in batch)
        entered.set()
        release.wait()

    monkeypatch.setattr(handler, "_write_batch", block_batch)
    try:
        assert handler._write_non_blocking(
            LogEntry("info", "first", tmp_path / "bounded.log")
        ) is True
        assert entered.wait(timeout=1)
        assert handler._write_non_blocking(
            LogEntry("info", "queued", tmp_path / "bounded.log")
        ) is True
        assert handler._write_non_blocking(
            LogEntry("info", "rejected", tmp_path / "bounded.log")
        ) is False
        release.set()
        started_at = time.monotonic()
        assert handler.shutdown(timeout=1) is True
        assert time.monotonic() - started_at < 1
        assert written == ["first", "queued"]
    finally:
        release.set()
        handler.shutdown(timeout=1)
        NonBlockingFileHandler._instance = original_instance


def test_non_blocking_file_handler_bounds_handler_close(monkeypatch, tmp_path):
    """文件处理器 close 阻塞时也必须保留关闭线程并支持最终重试。"""
    original_instance = NonBlockingFileHandler._instance
    NonBlockingFileHandler._instance = None
    handler = NonBlockingFileHandler()
    close_entered = threading.Event()
    close_release = threading.Event()
    log_handler = MagicMock()

    def block_close():
        """模拟文件系统在 flush 后阻塞关闭句柄。"""
        close_entered.set()
        close_release.wait()

    log_handler.close.side_effect = block_close
    log_path = tmp_path / "close.log"
    handler._rotating_handlers = {log_path: log_handler}
    try:
        assert handler.shutdown(timeout=0.05) is False
        assert close_entered.wait(timeout=1)
        assert handler._close_thread is not None
        assert handler._close_thread.is_alive()
        assert handler._rotating_handlers[log_path] is log_handler
        close_release.set()
        assert handler.shutdown(timeout=1) is True
        assert handler._rotating_handlers == {}
    finally:
        close_release.set()
        handler.shutdown(timeout=1)
        NonBlockingFileHandler._instance = original_instance


def test_logger_manager_retains_nonconverged_writer_for_retry(tmp_path):
    """平台日志门面不得在底层 writer 未收敛时丢失其 owner。"""
    previous_writer = LoggerManager._writer
    previous_log_path = LoggerManager._log_path
    writer = MagicMock()
    writer.shutdown.side_effect = [False, True]
    LoggerManager._writer = writer
    LoggerManager._log_path = tmp_path
    try:
        assert LoggerManager.shutdown() is False
        assert LoggerManager._writer is writer
        assert LoggerManager._log_path == tmp_path

        assert LoggerManager.shutdown() is True
        assert LoggerManager._writer is None
        assert LoggerManager._log_path is None
        assert writer.shutdown.call_count == 2
    finally:
        LoggerManager._writer = previous_writer
        LoggerManager._log_path = previous_log_path


def test_logger_manager_refuses_to_replace_nonconverged_writer(tmp_path):
    """重新装配不得用新 writer 覆盖仍持有资源的旧 owner。"""
    original_writer = LoggerManager._writer
    original_log_path = LoggerManager._log_path
    previous_writer = MagicMock()
    previous_writer.shutdown.return_value = False
    replacement_writer = MagicMock()
    old_path = tmp_path / "old"
    LoggerManager._writer = previous_writer
    LoggerManager._log_path = old_path
    try:
        with pytest.raises(RuntimeError, match="既有日志写入器未收敛"):
            LoggerManager.configure_writer(replacement_writer, tmp_path / "new")

        assert LoggerManager._writer is previous_writer
        assert LoggerManager._log_path == old_path
        replacement_writer.write_log.assert_not_called()
    finally:
        LoggerManager._writer = original_writer
        LoggerManager._log_path = original_log_path
