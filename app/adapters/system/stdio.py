from __future__ import annotations

import io
import logging
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path


class RotatingLineStream(io.TextIOBase):
    """
    将 stdout/stderr 按行写入滚动日志文件。

    这里不复用业务 logger，避免 stdout 日志再次回流到控制台或普通业务日志文件，
    同时保证启动阶段的 print/uvicorn 输出也能按配置滚动。
    """

    def __init__(self, log_file: Path, max_bytes: int, backup_count: int):
        """创建写入指定滚动日志文件的文本流。"""
        super().__init__()
        self._buffer = ""
        self._lock = threading.Lock()

        logger_name = f"moviepilot-stdio::{log_file}"
        self._logger = logging.getLogger(logger_name)
        self._logger.setLevel(logging.INFO)
        self._logger.propagate = False
        self._logger.handlers.clear()

        handler = RotatingFileHandler(
            filename=str(log_file),
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(message)s"))
        self._logger.addHandler(handler)

    @property
    def encoding(self) -> str:
        """返回日志流使用的文本编码。"""
        return "utf-8"

    def writable(self) -> bool:
        """声明该文本流支持写入。"""
        return True

    def isatty(self) -> bool:
        """声明该日志流不是交互式终端。"""
        return False

    def write(self, message: str) -> int:
        """缓冲消息并按完整行写入滚动日志。"""
        if not message:
            return 0

        with self._lock:
            self._buffer += message.replace("\r\n", "\n")
            while "\n" in self._buffer:
                line, self._buffer = self._buffer.split("\n", 1)
                self._logger.info(line)
        return len(message)

    def flush(self) -> None:
        """写出剩余缓冲区并刷新底层处理器。"""
        with self._lock:
            if self._buffer:
                self._logger.info(self._buffer)
                self._buffer = ""
            for handler in self._logger.handlers:
                handler.flush()


def configure_rotating_stdio(
    *, log_file: Path, max_bytes: int, backup_count: int
) -> RotatingLineStream:
    """
    将当前进程的 stdout/stderr 统一重定向到同一个滚动日志流。
    """

    log_file.parent.mkdir(parents=True, exist_ok=True)
    stream = RotatingLineStream(
        log_file=log_file,
        max_bytes=max_bytes,
        backup_count=backup_count,
    )
    sys.stdout = stream
    sys.stderr = stream
    return stream
