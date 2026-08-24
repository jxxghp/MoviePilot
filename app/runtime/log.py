from __future__ import annotations

import asyncio
import logging
import os
import queue
import sys
import threading
import time
from collections import deque
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from types import FrameType
from typing import Any, Callable, Dict, Optional, Protocol, Self

import click
from pydantic import BaseModel, ConfigDict

# strict mypy 跳过第三方实现导入，因此无法在本文件解析 Pydantic 元类类型。
class LogConfigModel(BaseModel):  # type: ignore[misc]
    """描述日志级别、格式和文件写入策略。"""

    model_config = ConfigDict(extra="ignore")

    CONFIG_DIR: Optional[str] = None
    DEBUG: bool = False
    LOG_LEVEL: str = "INFO"
    LOG_MAX_FILE_SIZE: int = 5
    LOG_BACKUP_COUNT: int = 10
    LOG_CONSOLE_FORMAT: str = (
        "%(leveltext)s[%(name)s] %(asctime)s [%(correlation_id)s] %(message)s"
    )
    LOG_FILE_FORMAT: str = (
        "【%(levelname)s】%(asctime)s [%(correlation_id)s] - %(message)s"
    )
    ASYNC_FILE_QUEUE_SIZE: int = 1000
    # 保留历史配置解析兼容；协程环境文件日志已统一由单一有界队列 writer 执行。
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
        self.correlation_id = _get_log_correlation_id()


class LogWriter(Protocol):
    """平台日志门面使用的文件写入端口。"""

    def write_log(self, level: str, message: str, file_path: Path) -> None:
        """将一条日志写入指定文件。"""

    def shutdown(self) -> Optional[bool]:
        """排空待写日志并释放写入资源，未收敛时返回 False。"""


log_settings = LogSettings()
_correlation_id_provider: Callable[[], str | None] = lambda: None
_LOG_STOP_TIMEOUT_SECONDS = 10.0


def configure_correlation_id_provider(provider: Callable[[], str | None]) -> None:
    """由组合根注入日志关联 ID 读取端口，保持日志模块为依赖叶节点。"""
    global _correlation_id_provider
    _correlation_id_provider = provider


def _get_log_correlation_id() -> str:
    """读取当前关联 ID；未装配或无请求上下文时返回稳定占位符。"""
    return _correlation_id_provider() or "-"


class NonBlockingFileHandler:
    """使用后台队列和滚动文件处理器写入业务日志。"""

    _instance = None
    _lock = threading.Lock()
    _stop_sentinel = None

    def __new__(cls) -> Self:
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
        self._write_queue: queue.Queue[Optional[LogEntry]] = queue.Queue(
            maxsize=log_settings.ASYNC_FILE_QUEUE_SIZE,
        )
        self._stop_requested = threading.Event()
        self._running = True
        self._closed = False
        self._close_thread: Optional[threading.Thread] = None
        self._close_error: Optional[BaseException] = None
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
                # 文件日志属于 E1 观测数据；队列达到显式上限时不能再创建无界线程池旁路。
                return False
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
        record = logging.LogRecord(
            name="",
            level=getattr(logging, entry.level.upper(), logging.INFO),
            pathname="",
            lineno=0,
            msg=entry.message,
            args=(),
            exc_info=None,
        )
        created_at = entry.timestamp.timestamp()
        record.created = created_at
        record.msecs = (created_at - int(created_at)) * 1000
        record.correlation_id = entry.correlation_id
        return record

    def _batch_writer(self) -> None:
        """持续收集队列日志，并在停止哨兵后排空已有批次。"""
        while True:
            try:
                batch: list[LogEntry] = []
                should_stop = False
                end_time = time.monotonic() + log_settings.WRITE_TIMEOUT
                while (
                    len(batch) < log_settings.BATCH_WRITE_SIZE
                    and time.monotonic() < end_time
                ):
                    try:
                        if (
                            self._stop_requested.is_set()
                            and self._write_queue.empty()
                        ):
                            should_stop = True
                            break
                        remaining_time = max(0, end_time - time.monotonic())
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
                if self._stop_requested.is_set() and self._write_queue.empty():
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

    def _close_handlers(self) -> None:
        """在独立 owner 中关闭文件处理器，保留失败项供后续重试。"""
        first_error: Optional[BaseException] = None
        with self._handlers_lock:
            handlers = tuple(self._rotating_handlers.items())
        for file_path, handler in handlers:
            try:
                handler.flush()
                handler.close()
            except BaseException as err:  # noqa: BLE001  需要保留关闭失败 owner
                if first_error is None:
                    first_error = err
                print(f"日志处理器关闭失败 {file_path}: {err}")
                continue
            with self._handlers_lock:
                if self._rotating_handlers.get(file_path) is handler:
                    self._rotating_handlers.pop(file_path, None)
        with self._state_lock:
            self._close_error = first_error

    def _close_handlers_bounded(self, deadline: float) -> bool:
        """复用关停总预算有限等待文件处理器关闭 owner。"""
        with self._state_lock:
            if self._closed:
                return True
            close_thread = self._close_thread
            if close_thread is None:
                self._close_error = None
                close_thread = threading.Thread(
                    target=self._close_handlers,
                    daemon=True,
                    name="LogHandlerCloser",
                )
                self._close_thread = close_thread
                close_thread.start()
        if close_thread is threading.current_thread():
            return False
        close_thread.join(timeout=max(0.0, deadline - time.monotonic()))
        if close_thread.is_alive():
            return False
        with self._state_lock:
            if self._close_error is not None:
                if self._close_thread is close_thread:
                    self._close_thread = None
                return False
            self._closed = True
            return True

    def shutdown(
        self,
        timeout: float = _LOG_STOP_TIMEOUT_SECONDS,
    ) -> bool:
        """
        停止接收新日志，并在总预算内排空队列和关闭文件处理器。

        :param timeout: 等待写线程和文件处理器收敛的最长秒数
        :return: 全部日志资源真实终止时返回 True，否则返回 False
        """
        deadline = time.monotonic() + max(0.0, timeout)
        with self._state_lock:
            if self._closed:
                return True
            if self._running:
                self._running = False
                self._stop_requested.set()
                if self._write_thread.is_alive():
                    try:
                        self._write_queue.put_nowait(self._stop_sentinel)
                    except queue.Full:
                        # 队列非空会自然唤醒 writer；停止事件让其排空后退出。
                        pass
        if self._write_thread is threading.current_thread():
            return False
        self._write_thread.join(timeout=max(0.0, deadline - time.monotonic()))
        if self._write_thread.is_alive():
            return False
        return self._close_handlers_bounded(deadline)


_LEVEL_NAME_COLORS: dict[int, Callable[[str], str]] = {
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
        record.correlation_id = _get_log_correlation_id()
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

        虚拟实例共享物理源码，因此优先使用实例专属模块命名空间路由日志；
        无模块身份的旧插件仍按调用文件路径识别。
        插件调用宿主公共方法时，调用栈中仍保留插件帧，因此日志继续进入该插件
        的独立文件，而不是混入主程序日志。
        """
        caller_name = None
        plugin_name = None
        frame: Optional[FrameType]
        try:
            frame = sys._getframe(3)  # noqa: SLF001
        except (AttributeError, ValueError):
            return "log.py", None

        while frame:
            filepath = Path(frame.f_code.co_filename)
            parts = filepath.parts
            if not caller_name:
                caller_name = parts[-2] if parts[-1] == "__init__.py" and len(parts) >= 2 else parts[-1]
            module_name = frame.f_globals.get("__name__")
            if isinstance(module_name, str):
                module_parts = module_name.split(".")
                if (
                    len(module_parts) >= 3
                    and module_parts[:2] == ["app", "plugins"]
                    and module_parts[2]
                ):
                    plugin_name = module_parts[2]
                    break
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
        if previous_writer and previous_writer is not writer:
            if previous_writer.shutdown() is False:
                raise RuntimeError("既有日志写入器未收敛，拒绝丢失其资源 owner")
        with cls._lock:
            if cls._writer is not previous_writer:
                raise RuntimeError("日志写入器在装配期间被并发替换")
            cls._writer = writer
            cls._log_path = Path(log_path)
            pending = list(cls._pending_file_logs)
            cls._pending_file_logs.clear()
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
    def shutdown(cls) -> bool:
        """关闭当前文件写入器，未收敛时保留 owner 供后续重试。"""
        with cls._lock:
            writer = cls._writer
        if writer is None:
            return True
        if writer.shutdown() is False:
            return False
        with cls._lock:
            if cls._writer is writer:
                cls._writer = None
                cls._log_path = None
        return True


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
