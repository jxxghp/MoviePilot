from __future__ import annotations

import asyncio
import logging
import os
import queue
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict, Optional, Protocol

import click
from pydantic import BaseModel, ConfigDict


class LogConfigModel(BaseModel):
    """描述日志级别、格式和文件写入策略。"""

    model_config = ConfigDict(extra="ignore")

    CONFIG_DIR: Optional[str] = None
    DEBUG: bool = False
    LOG_LEVEL: str = "INFO"
    LOG_MAX_FILE_SIZE: int = 5
    LOG_BACKUP_COUNT: int = 10
    LOG_CONSOLE_FORMAT: str = "%(leveltext)s[%(name)s] %(asctime)s %(message)s"
    LOG_FILE_FORMAT: str = "【%(levelname)s】%(asctime)s - %(message)s"
    ASYNC_FILE_QUEUE_SIZE: int = 1000
    ASYNC_FILE_WORKERS: int = 2
    BATCH_WRITE_SIZE: int = 50
    WRITE_TIMEOUT: float = 3.0

    @property
    def LOG_MAX_FILE_SIZE_BYTES(self) -> int:
        """将日志文件大小从 MB 转换为字节。"""
        return self.LOG_MAX_FILE_SIZE * 1024 * 1024


class LogSettings(LogConfigModel):
    """保存当前进程已经生效的日志策略。"""


class LogEntry:
    """表示一条等待基础设施写入的文件日志。"""

    def __init__(
        self,
        level: str,
        message: str,
        file_path: Path,
        timestamp: datetime | None = None,
    ) -> None:
        """记录日志级别、格式化文本和目标文件。"""
        self.level = level
        self.message = message
        self.file_path = file_path
        self.timestamp = timestamp or datetime.now()


class LogWriter(Protocol):
    """平台日志门面使用的文件写入端口。"""

    def write_log(self, level: str, message: str, file_path: Path) -> None:
        """将一条日志写入指定文件。"""

    def shutdown(self) -> None:
        """排空待写日志并释放写入资源。"""


log_settings = LogSettings()


class NonBlockingFileHandler:
    """使用后台队列和滚动文件处理器写入业务日志。"""

    _instance = None
    _lock = threading.Lock()
    _stop_sentinel = object()

    def __new__(cls):
        """返回进程内唯一的文件写入器。"""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self) -> None:
        """初始化文件处理器缓存、异步队列和后台写线程。"""
        if hasattr(self, "_initialized"):
            return
        self._initialized = True
        self._state_lock = threading.RLock()
        self._handlers_lock = threading.Lock()
        self._rotating_handlers: dict[Path, RotatingFileHandler] = {}
        self._write_queue = queue.Queue(maxsize=log_settings.ASYNC_FILE_QUEUE_SIZE)
        self._executor = ThreadPoolExecutor(
            max_workers=log_settings.ASYNC_FILE_WORKERS,
            thread_name_prefix="LogWriter",
        )
        self._running = True
        self._write_thread = threading.Thread(
            target=self._batch_writer,
            daemon=True,
            name="LogBatchWriter",
        )
        self._write_thread.start()

    def _get_rotating_handler(self, file_path: Path) -> RotatingFileHandler:
        """按目标路径复用滚动文件处理器。"""
        with self._handlers_lock:
            handler = self._rotating_handlers.get(file_path)
            if handler:
                return handler
            file_path.parent.mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(
                filename=str(file_path),
                maxBytes=log_settings.LOG_MAX_FILE_SIZE_BYTES,
                backupCount=log_settings.LOG_BACKUP_COUNT,
                encoding="utf-8",
            )
            handler.setFormatter(logging.Formatter(log_settings.LOG_FILE_FORMAT))
            self._rotating_handlers[file_path] = handler
            return handler

    def write_log(self, level: str, message: str, file_path: Path) -> None:
        """根据当前线程是否运行事件循环选择异步或同步文件写入。"""
        entry = LogEntry(level, message, file_path)
        if self._is_in_event_loop():
            self._write_non_blocking(entry)
            return
        with self._state_lock:
            if self._running:
                self._write_sync(entry)

    @staticmethod
    def _is_in_event_loop() -> bool:
        """判断当前线程是否正在运行 asyncio 事件循环。"""
        try:
            return asyncio.get_running_loop() is not None
        except RuntimeError:
            return False

    def _write_non_blocking(self, entry: LogEntry) -> bool:
        """将协程环境产生的日志无阻塞地放入写队列。"""
        with self._state_lock:
            if not self._running:
                return False
            try:
                self._write_queue.put_nowait(entry)
            except queue.Full:
                self._executor.submit(self._write_sync, entry)
            return True

    def _write_sync(self, entry: LogEntry) -> None:
        """立即将单条日志写入滚动文件。"""
        try:
            handler = self._get_rotating_handler(entry.file_path)
            handler.handle(self._to_record(entry))
        except Exception as err:
            print(f"日志写入失败 {entry.file_path}: {err}")
            print(f"【{entry.level.upper()}】{entry.timestamp} - {entry.message}")

    @staticmethod
    def _to_record(entry: LogEntry) -> logging.LogRecord:
        """把日志条目转换为标准库日志记录。"""
        return logging.LogRecord(
            name="",
            level=getattr(logging, entry.level.upper(), logging.INFO),
            pathname="",
            lineno=0,
            msg=entry.message,
            args=(),
            exc_info=None,
            created=entry.timestamp.timestamp(),
        )

    def _batch_writer(self) -> None:
        """持续收集队列日志，并在停止哨兵后排空已有批次。"""
        while True:
            try:
                batch = []
                should_stop = False
                end_time = time.time() + log_settings.WRITE_TIMEOUT
                while (
                    len(batch) < log_settings.BATCH_WRITE_SIZE
                    and time.time() < end_time
                ):
                    try:
                        remaining_time = max(0, end_time - time.time())
                        entry = self._write_queue.get(timeout=remaining_time)
                        if entry is self._stop_sentinel:
                            should_stop = True
                            break
                        batch.append(entry)
                    except queue.Empty:
                        break
                if batch:
                    self._write_batch(batch)
                if should_stop:
                    break
            except Exception as err:
                print(f"批量写入线程错误: {err}")
                time.sleep(0.1)

    def _write_batch(self, batch: list[LogEntry]) -> None:
        """按目标文件分组写入一批日志。"""
        file_groups: dict[Path, list[LogEntry]] = {}
        for entry in batch:
            file_groups.setdefault(entry.file_path, []).append(entry)
        for file_path, entries in file_groups.items():
            try:
                handler = self._get_rotating_handler(file_path)
                for entry in entries:
                    handler.handle(self._to_record(entry))
            except Exception as err:
                print(f"批量写入失败 {file_path}: {err}")
                for entry in entries:
                    self._write_sync(entry)

    def shutdown(self) -> None:
        """停止接收新日志，排空队列并关闭线程池和文件处理器。"""
        with self._state_lock:
            if not self._running:
                return
            self._running = False
            if self._write_thread.is_alive():
                self._write_queue.put(self._stop_sentinel)
        if self._write_thread.is_alive():
            self._write_thread.join()
        self._executor.shutdown(wait=True)
        for handler in self._rotating_handlers.values():
            handler.flush()
            handler.close()
        self._rotating_handlers.clear()

_LEVEL_NAME_COLORS = {
    logging.DEBUG: lambda level_name: click.style(str(level_name), fg="cyan"),
    logging.INFO: lambda level_name: click.style(str(level_name), fg="green"),
    logging.WARNING: lambda level_name: click.style(str(level_name), fg="yellow"),
    logging.ERROR: lambda level_name: click.style(str(level_name), fg="red"),
    logging.CRITICAL: lambda level_name: click.style(
        str(level_name), fg="bright_red"
    ),
}


class CustomFormatter(logging.Formatter):
    """为控制台日志级别添加颜色和对齐文本。"""

    def format(self, record: logging.LogRecord) -> str:
        """格式化一条控制台日志记录。"""
        separator = " " * max(8 - len(record.levelname), 0)
        colorizer = _LEVEL_NAME_COLORS.get(record.levelno, str)
        record.leveltext = colorizer(record.levelname + ":") + separator
        return super().format(record)


class LoggerManager:
    """
    管理进程级日志策略、控制台输出和插件日志路由。

    文件 I/O 由启动层注入的 :class:`LogWriter` 完成；在写入器装配之前产生的
    少量启动日志会暂存，装配完成后只补写文件，不重复输出到控制台。
    """

    _loggers: Dict[str, logging.Logger] = {}
    _default_log_file = Path("moviepilot.log")
    _lock = threading.RLock()
    _writer: Optional[LogWriter] = None
    _log_path: Optional[Path] = None
    _pending_file_logs: deque[tuple[str, str, Path]] = deque(maxlen=1000)

    def get_logger(self, name: str) -> logging.Logger:
        """返回使用当前控制台策略的命名标准库日志器。"""
        logfile = Path(f"{name}.log")
        return self._get_console_logger(logfile)

    @staticmethod
    def _get_caller() -> tuple[str, Optional[str]]:
        """
        识别日志调用文件和插件来源。

        插件调用宿主公共方法时，调用栈中仍保留插件帧，因此日志继续进入该插件
        的独立文件，而不是混入主程序日志。
        """
        caller_name = None
        plugin_name = None
        try:
            frame = sys._getframe(3)  # noqa: SLF001
        except (AttributeError, ValueError):
            return "log.py", None

        while frame:
            filepath = Path(frame.f_code.co_filename)
            parts = filepath.parts
            if not caller_name:
                caller_name = parts[-2] if parts[-1] == "__init__.py" and len(parts) >= 2 else parts[-1]
            if "app" in parts:
                if not plugin_name and "plugins" in parts:
                    try:
                        plugins_index = parts.index("plugins")
                        if plugins_index + 1 < len(parts):
                            plugin_name = parts[plugins_index + 1]
                            break
                    except ValueError:
                        pass
                if "main.py" in parts:
                    break
            elif len(parts) != 1:
                break
            frame = frame.f_back
        return caller_name or "log.py", plugin_name

    @classmethod
    def _setup_console_logger(cls, logfile: Path) -> logging.Logger:
        """创建只负责控制台输出的标准库日志器。"""
        logger_name = str(logfile.with_suffix(""))
        configured_logger = logging.getLogger(logger_name)
        configured_logger.setLevel(cls._get_log_level())
        configured_logger.handlers.clear()
        if os.getenv("MOVIEPILOT_DISABLE_CONSOLE_LOG") != "1":
            console_handler = logging.StreamHandler()
            console_handler.setFormatter(
                CustomFormatter(log_settings.LOG_CONSOLE_FORMAT)
            )
            configured_logger.addHandler(console_handler)
        configured_logger.propagate = False
        return configured_logger

    @classmethod
    def _get_console_logger(cls, logfile: Path) -> logging.Logger:
        """按日志文件语境复用控制台日志器。"""
        logger_key = str(logfile)
        with cls._lock:
            configured_logger = cls._loggers.get(logger_key)
            if not configured_logger:
                configured_logger = cls._setup_console_logger(logfile)
                cls._loggers[logger_key] = configured_logger
            return configured_logger

    @classmethod
    def configure_writer(cls, writer: LogWriter, log_path: Path) -> None:
        """装配文件写入器，并补写装配前暂存的启动日志。"""
        with cls._lock:
            previous_writer = cls._writer
            cls._writer = writer
            cls._log_path = Path(log_path)
            pending = list(cls._pending_file_logs)
            cls._pending_file_logs.clear()
        if previous_writer and previous_writer is not writer:
            previous_writer.shutdown()
        for level, message, logfile in pending:
            writer.write_log(level, message, Path(log_path) / logfile)

    def update_loggers(self) -> None:
        """让已创建的控制台日志器应用最新级别和格式。"""
        with self._lock:
            for configured_logger in self._loggers.values():
                for handler in configured_logger.handlers:
                    if isinstance(handler, logging.StreamHandler):
                        handler.setFormatter(
                            CustomFormatter(log_settings.LOG_CONSOLE_FORMAT)
                        )
                configured_logger.setLevel(self._get_log_level())

    @staticmethod
    def _get_log_level() -> int:
        """返回当前日志策略对应的标准库日志级别。"""
        if log_settings.DEBUG:
            return logging.DEBUG
        return getattr(logging, log_settings.LOG_LEVEL.upper(), logging.INFO)

    @classmethod
    def _write_file_log(cls, level: str, message: str, logfile: Path) -> None:
        """写入文件，或在启动装配完成前暂存日志。"""
        with cls._lock:
            writer = cls._writer
            log_path = cls._log_path
            if not writer or not log_path:
                cls._pending_file_logs.append((level, message, logfile))
                return
        writer.write_log(level, message, log_path / logfile)

    def logger(self, method: str, msg: str, *args: Any, **kwargs: Any) -> None:
        """按调用来源路由并输出一条日志。"""
        method_level = getattr(logging, method.upper(), logging.INFO)
        if method_level < self._get_log_level():
            return

        caller_name, plugin_name = self._get_caller()
        formatted_msg = f"{caller_name} - {msg}"
        if args:
            try:
                formatted_msg = formatted_msg % args
            except (TypeError, ValueError):
                formatted_msg = f"{formatted_msg} {' '.join(str(arg) for arg in args)}"

        logfile = (
            Path("plugins") / f"{plugin_name}.log"
            if plugin_name
            else self._default_log_file
        )
        self._write_file_log(method.upper(), formatted_msg, logfile)

        configured_logger = self._get_console_logger(logfile)
        log_method = getattr(configured_logger, method, configured_logger.info)
        log_method(formatted_msg, **kwargs)

    def info(self, msg: str, *args: Any, **kwargs: Any) -> None:
        """输出 INFO 日志。"""
        self.logger("info", msg, *args, **kwargs)

    def debug(self, msg: str, *args: Any, **kwargs: Any) -> None:
        """输出 DEBUG 日志。"""
        self.logger("debug", msg, *args, **kwargs)

    def warning(self, msg: str, *args: Any, **kwargs: Any) -> None:
        """输出 WARNING 日志。"""
        self.logger("warning", msg, *args, **kwargs)

    def warn(self, msg: str, *args: Any, **kwargs: Any) -> None:
        """兼容历史调用并输出 WARNING 日志。"""
        self.warning(msg, *args, **kwargs)

    def error(self, msg: str, *args: Any, **kwargs: Any) -> None:
        """输出 ERROR 日志。"""
        self.logger("error", msg, *args, **kwargs)

    def critical(self, msg: str, *args: Any, **kwargs: Any) -> None:
        """输出 CRITICAL 日志。"""
        self.logger("critical", msg, *args, **kwargs)

    @classmethod
    def shutdown(cls) -> None:
        """断开并关闭当前文件写入器。"""
        with cls._lock:
            writer = cls._writer
            cls._writer = None
            cls._log_path = None
        if writer:
            writer.shutdown()


logger = LoggerManager()


def configure_log_settings(source: object) -> None:
    """从完整系统配置同步日志相关字段并刷新控制台策略。"""
    for field_name in LogConfigModel.model_fields:
        if hasattr(source, field_name):
            setattr(log_settings, field_name, getattr(source, field_name))
    logger.update_loggers()


def configure_log_writer(writer: LogWriter, log_path: Path) -> None:
    """把基础设施文件写入器装配到平台日志门面。"""
    LoggerManager.configure_writer(writer=writer, log_path=log_path)
