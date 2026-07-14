import threading
import time
from unittest.mock import MagicMock

from app.log import LogEntry, NonBlockingFileHandler, log_settings


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

    monkeypatch.setattr("app.log.RotatingFileHandler", ProbeHandler)
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
