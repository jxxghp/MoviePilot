import asyncio
import logging
import queue
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional

import click
from pydantic import BaseSettings, BaseModel

from app.utils.system import SystemUtils


class LogConfigModel(BaseModel):
    """
    Pydantic 配置模型，描述所有配置项及其类型和默认值
    """

    class Config:
        extra = "ignore"  # 忽略未定义的配置项

    # 配置文件目录
    CONFIG_DIR: Optional[str] = None
    # 是否为调试模式
    DEBUG: bool = False
    # 日志级别（DEBUG、INFO、WARNING、ERROR等）
    LOG_LEVEL: str = "INFO"
    # 日志文件最大大小（单位：MB）
    LOG_MAX_FILE_SIZE: int = 5
    # 备份的日志文件数量
    LOG_BACKUP_COUNT: int = 10
    # 控制台日志格式
    LOG_CONSOLE_FORMAT: str = "%(leveltext)s[%(name)s] %(asctime)s %(message)s"
    # 文件日志格式
    LOG_FILE_FORMAT: str = "【%(levelname)s】%(asctime)s - %(message)s"
    # 异步文件写入队列大小
    ASYNC_FILE_QUEUE_SIZE: int = 1000
    # 异步文件写入线程数
    ASYNC_FILE_WORKERS: int = 2
    # 批量写入大小
    BATCH_WRITE_SIZE: int = 50
    # 写入超时时间（秒）
    WRITE_TIMEOUT: float = 3.0


class LogSettings(BaseSettings, LogConfigModel):
    """
    日志设置类
    """

    @property
    def CONFIG_PATH(self):
        return SystemUtils.get_config_path(self.CONFIG_DIR)

    @property
    def LOG_PATH(self):
        """
        获取日志存储路径
        """
        return self.CONFIG_PATH / "logs"

    @property
    def LOG_MAX_FILE_SIZE_BYTES(self):
        """
        将日志文件大小转换为字节（MB -> Bytes）
        """
        return self.LOG_MAX_FILE_SIZE * 1024 * 1024

    class Config:
        case_sensitive = True
        env_file = SystemUtils.get_env_path()
        env_file_encoding = "utf-8"


# 实例化日志设置
log_settings = LogSettings()

# 日志级别颜色映射
level_name_colors = {
    logging.DEBUG: lambda level_name: click.style(str(level_name), fg="cyan"),
    logging.INFO: lambda level_name: click.style(str(level_name), fg="green"),
    logging.WARNING: lambda level_name: click.style(str(level_name), fg="yellow"),
    logging.ERROR: lambda level_name: click.style(str(level_name), fg="red"),
    logging.CRITICAL: lambda level_name: click.style(str(level_name), fg="bright_red"),
}


class CustomFormatter(logging.Formatter):
    """
    自定义日志输出格式
    """

    def __init__(self, fmt=None):
        super().__init__(fmt)

    def format(self, record):
        separator = " " * (8 - len(record.levelname))
        record.leveltext = level_name_colors[record.levelno](record.levelname + ":") + separator
        return super().format(record)


class LogEntry:
    """
    日志条目
    """

    def __init__(self, level: str, message: str, file_path: Path, timestamp: datetime = None):
        self.level = level
        self.message = message
        self.file_path = file_path
        self.timestamp = timestamp or datetime.now()


class NonBlockingFileHandler:
    """
    非阻塞文件处理器 - 透明地处理协程环境中的文件写入
    """
    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if hasattr(self, '_initialized'):
            return

        self._initialized = True
        self._write_queue = queue.Queue(maxsize=log_settings.ASYNC_FILE_QUEUE_SIZE)
        self._executor = ThreadPoolExecutor(max_workers=log_settings.ASYNC_FILE_WORKERS,
                                            thread_name_prefix="LogWriter")
        self._batch_buffer = {}
        self._last_flush = {}
        self._running = True

        # 启动后台写入线程
        self._write_thread = threading.Thread(target=self._batch_writer, daemon=True)
        self._write_thread.start()

    def write_log(self, level: str, message: str, file_path: Path):
        """
        写入日志 - 自动检测协程环境并使用合适的方式
        """
        entry = LogEntry(level, message, file_path)

        # 检测是否在协程环境中
        if self._is_in_event_loop():
            # 在协程环境中，使用非阻塞方式
            self._write_non_blocking(entry)
        else:
            # 不在协程环境中，直接同步写入
            self._write_sync(entry)

    @staticmethod
    def _is_in_event_loop() -> bool:
        """
        检测当前是否在事件循环中
        """
        try:
            loop = asyncio.get_running_loop()
            return loop is not None
        except RuntimeError:
            return False

    def _write_non_blocking(self, entry: LogEntry):
        """
        非阻塞写入（用于协程环境）
        """
        try:
            self._write_queue.put_nowait(entry)
        except queue.Full:
            # 队列满时，使用线程池处理
            self._executor.submit(self._write_sync, entry)

    @staticmethod
    def _write_sync(entry: LogEntry):
        """
        同步写入日志
        """
        try:
            # 确保目录存在
            entry.file_path.parent.mkdir(parents=True, exist_ok=True)

            # 格式化时间戳
            timestamp = entry.timestamp.strftime('%Y-%m-%d %H:%M:%S,') + f"{entry.timestamp.microsecond // 1000:03d}"
            line = f"【{entry.level.upper()}】{timestamp} - {entry.message}\n"

            # 写入文件
            with open(entry.file_path, 'a', encoding='utf-8') as f:
                f.write(line)
        except Exception as e:
            # 如果文件写入失败，至少输出到控制台
            print(f"日志写入失败 {entry.file_path}: {e}")
            print(f"【{entry.level.upper()}】{entry.timestamp} - {entry.message}")

    def _batch_writer(self):
        """
        后台批量写入线程
        """
        while self._running:
            try:
                # 收集一批日志条目
                batch = []
                end_time = time.time() + log_settings.WRITE_TIMEOUT

                while len(batch) < log_settings.BATCH_WRITE_SIZE and time.time() < end_time:
                    try:
                        remaining_time = max(0, end_time - time.time())
                        entry = self._write_queue.get(timeout=remaining_time)
                        batch.append(entry)
                    except queue.Empty:
                        break

                if batch:
                    self._write_batch(batch)

            except Exception as e:
                print(f"批量写入线程错误: {e}")
                time.sleep(0.1)

    def _write_batch(self, batch: list):
        """
        批量写入日志
        """
        # 按文件分组
        file_groups = {}
        for entry in batch:
            if entry.file_path not in file_groups:
                file_groups[entry.file_path] = []
            file_groups[entry.file_path].append(entry)

        # 批量写入每个文件
        for file_path, entries in file_groups.items():
            try:
                # 确保目录存在
                file_path.parent.mkdir(parents=True, exist_ok=True)

                # 批量写入
                with open(file_path, 'a', encoding='utf-8') as f:
                    for entry in entries:
                        timestamp = entry.timestamp.strftime(
                            '%Y-%m-%d %H:%M:%S,') + f"{entry.timestamp.microsecond // 1000:03d}"
                        line = f"【{entry.level.upper()}】{timestamp} - {entry.message}\n"
                        f.write(line)
            except Exception as e:
                print(f"批量写入失败 {file_path}: {e}")
                # 回退到逐个写入
                for entry in entries:
                    self._write_sync(entry)

    def shutdown(self):
        """
        关闭文件处理器
        """
        self._running = False
        if hasattr(self, '_write_thread'):
            self._write_thread.join(timeout=5)
        if self._executor:
            self._executor.shutdown(wait=True)


class LoggerManager:
    """
    日志管理
    """
    # 管理所有的 Logger
    _loggers: Dict[str, Any] = {}
    # 默认日志文件名称
    _default_log_file = "moviepilot.log"
    # 线程锁
    _lock = threading.Lock()
    # 非阻塞文件处理器
    _file_handler = NonBlockingFileHandler()

    def get_logger(self, name: str) -> logging.Logger:
        """
        获取一个指定名称的、独立的日志记录器。
        创建一个独立的日志文件，例如 'diag_memory.log'。
        :param name: 日志记录器的名称，也将用作文件名。
        :return: 一个配置好的 logging.Logger 实例。
        """
        # 使用名称作为日志文件名
        logfile = f"{name}.log"
        with LoggerManager._lock:
            # 检查是否已经创建过这个 logger
            _logger = self._loggers.get(logfile)
            if not _logger:
                # 如果没有，就使用现有的 __setup_console_logger 来创建一个新的
                _logger = self.__setup_console_logger(log_file=logfile)
                self._loggers[logfile] = _logger
        return _logger

    @staticmethod
    def __get_caller():
        """
        获取调用者的文件名称与插件名称
        如果是插件调用内置的模块, 也能写入到插件日志文件中
        """
        # 调用者文件名称
        caller_name = None
        # 调用者插件名称
        plugin_name = None

        try:
            frame = sys._getframe(3)  # noqa
        except (AttributeError, ValueError):
            # 如果无法获取帧，返回默认值
            return "log.py", None

        while frame:
            filepath = Path(frame.f_code.co_filename)
            parts = filepath.parts
            # 设定调用者文件名称
            if not caller_name:
                if parts[-1] == "__init__.py" and len(parts) >= 2:
                    caller_name = parts[-2]
                else:
                    caller_name = parts[-1]
            # 设定调用者插件名称
            if "app" in parts:
                if not plugin_name and "plugins" in parts:
                    try:
                        plugins_index = parts.index("plugins")
                        if plugins_index + 1 < len(parts):
                            plugin_candidate = parts[plugins_index + 1]
                            if plugin_candidate == "__init__.py":
                                plugin_name = "plugin"
                            else:
                                plugin_name = plugin_candidate
                            break
                    except ValueError:
                        pass
                if "main.py" in parts:
                    # 已经到达程序的入口，停止遍历
                    break
            elif len(parts) != 1:
                # 已经超出程序范围，停止遍历
                break
            # 获取上一个帧
            try:
                frame = frame.f_back
            except AttributeError:
                break
        return caller_name or "log.py", plugin_name

    @staticmethod
    def __setup_console_logger(log_file: str):
        """
        初始化控制台日志实例（文件输出由 NonBlockingFileHandler 处理）
        :param log_file：日志文件相对路径
        """
        log_file_path = log_settings.LOG_PATH / log_file

        # 创建新实例
        _logger = logging.getLogger(log_file_path.stem)

        # 设置日志级别
        _logger.setLevel(LoggerManager.__get_log_level())

        # 移除已有的 handler，避免重复添加
        for handler in _logger.handlers:
            _logger.removeHandler(handler)

        # 只设置终端日志（文件日志由 NonBlockingFileHandler 处理）
        console_handler = logging.StreamHandler()
        console_formatter = CustomFormatter(log_settings.LOG_CONSOLE_FORMAT)
        console_handler.setFormatter(console_formatter)
        _logger.addHandler(console_handler)

        # 禁止向父级log传递
        _logger.propagate = False

        return _logger

    def update_loggers(self):
        """
        更新日志实例
        """
        with LoggerManager._lock:
            for _logger in self._loggers.values():
                self.__update_logger_handlers(_logger)

    @staticmethod
    def __update_logger_handlers(_logger: logging.Logger):
        """
        更新 Logger 的 handler 配置
        :param _logger: 需要更新的 Logger 实例
        """
        # 更新现有 handler（只有控制台 handler）
        for handler in _logger.handlers:
            try:
                if isinstance(handler, logging.StreamHandler):
                    # 更新控制台输出格式
                    console_formatter = CustomFormatter(log_settings.LOG_CONSOLE_FORMAT)
                    handler.setFormatter(console_formatter)
            except Exception as e:
                print(f"更新日志处理器失败: {handler}. 错误: {e}")
        # 更新日志级别
        _logger.setLevel(LoggerManager.__get_log_level())

    @staticmethod
    def __get_log_level():
        """
        获取当前日志级别
        """
        return logging.DEBUG if log_settings.DEBUG else getattr(logging, log_settings.LOG_LEVEL.upper(), logging.INFO)

    def logger(self, method: str, msg: str, *args, **kwargs):
        """
        获取模块的logger
        :param method: 日志方法
        :param msg: 日志信息
        """
        # 获取当前日志级别
        current_level = self.__get_log_level()
        method_level = getattr(logging, method.upper(), logging.INFO)

        # 如果当前方法的级别低于设定的日志级别，则不处理
        if method_level < current_level:
            return

        # 获取调用者文件名和插件名
        caller_name, plugin_name = self.__get_caller()

        # 格式化消息
        formatted_msg = f"{caller_name} - {msg}"
        if args:
            try:
                formatted_msg = formatted_msg % args
            except (TypeError, ValueError):
                # 如果格式化失败，直接拼接
                formatted_msg = f"{formatted_msg} {' '.join(str(arg) for arg in args)}"

        # 区分插件日志
        if plugin_name:
            # 使用插件日志文件
            logfile = Path("plugins") / f"{plugin_name}.log"
        else:
            # 使用默认日志文件
            logfile = self._default_log_file

        # 构建完整的日志文件路径
        log_file_path = log_settings.LOG_PATH / logfile

        # 使用非阻塞文件处理器写入文件日志
        self._file_handler.write_log(method.upper(), formatted_msg, log_file_path)

        # 同时保持控制台输出（使用标准 logging）
        with LoggerManager._lock:
            _logger = self._loggers.get(logfile)
            if not _logger:
                _logger = self.__setup_console_logger(log_file=logfile)
                self._loggers[logfile] = _logger

        # 只在控制台输出，文件写入已由 _file_handler 处理
        if hasattr(_logger, method):
            log_method = getattr(_logger, method)
            log_method(formatted_msg)

    def info(self, msg: str, *args, **kwargs):
        """
        输出信息级别日志
        """
        self.logger("info", msg, *args, **kwargs)

    def debug(self, msg: str, *args, **kwargs):
        """
        输出调试级别日志
        """
        self.logger("debug", msg, *args, **kwargs)

    def warning(self, msg: str, *args, **kwargs):
        """
        输出警告级别日志
        """
        self.logger("warning", msg, *args, **kwargs)

    def warn(self, msg: str, *args, **kwargs):
        """
        输出警告级别日志（兼容）
        """
        self.warning(msg, *args, **kwargs)

    def error(self, msg: str, *args, **kwargs):
        """
        输出错误级别日志
        """
        self.logger("error", msg, *args, **kwargs)

    def critical(self, msg: str, *args, **kwargs):
        """
        输出严重错误级别日志
        """
        self.logger("critical", msg, *args, **kwargs)

    @classmethod
    def shutdown(cls):
        """
        关闭日志管理器，清理资源
        """
        if cls._file_handler:
            cls._file_handler.shutdown()


# 初始化日志管理
logger = LoggerManager()
