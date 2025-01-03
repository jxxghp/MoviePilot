import inspect
import logging
from logging.handlers import RotatingFileHandler
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
    LOG_BACKUP_COUNT: int = 3
    # 控制台日志格式
    LOG_CONSOLE_FORMAT: str = "%(leveltext)s%(message)s"
    # 文件日志格式
    LOG_FILE_FORMAT: str = "【%(levelname)s】%(asctime)s - %(message)s"


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


class LoggerManager:
    """
    日志管理
    """
    # 管理所有的 Logger
    _loggers: Dict[str, Any] = {}
    # 默认日志文件名称
    _default_log_file = "moviepilot.log"

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
        for i in inspect.stack()[3:]:
            filepath = Path(i.filename)
            parts = filepath.parts
            if not caller_name:
                # 设定调用者文件名称
                if parts[-1] == "__init__.py":
                    caller_name = parts[-2]
                else:
                    caller_name = parts[-1]
            if "app" in parts:
                if not plugin_name and "plugins" in parts:
                    # 设定调用者插件名称
                    plugin_name = parts[parts.index("plugins") + 1]
                    if plugin_name == "__init__.py":
                        plugin_name = "plugin"
                    break
                if "main.py" in parts:
                    # 已经到达程序的入口
                    break
            elif len(parts) != 1:
                # 已经超出程序范围
                break
        return caller_name or "log.py", plugin_name

    @staticmethod
    def __setup_logger(log_file: str):
        """
        设置日志

        :param log_file：日志文件相对路径
        """
        log_file_path = log_settings.LOG_PATH / log_file
        log_file_path.parent.mkdir(parents=True, exist_ok=True)

        # 创建新实例
        _logger = logging.getLogger(log_file_path.stem)

        if log_settings.DEBUG:
            _logger.setLevel(logging.DEBUG)

        # 全局日志等级
        else:
            loglevel = getattr(logging, log_settings.LOG_LEVEL.upper(), logging.INFO)
            _logger.setLevel(loglevel)

        # 移除已有的 handler，避免重复添加
        for handler in _logger.handlers:
            _logger.removeHandler(handler)

        # 终端日志
        console_handler = logging.StreamHandler()
        console_formatter = CustomFormatter(log_settings.LOG_CONSOLE_FORMAT)
        console_handler.setFormatter(console_formatter)
        _logger.addHandler(console_handler)

        # 文件日志
        file_handler = RotatingFileHandler(
            filename=log_file_path,
            mode="a",
            maxBytes=log_settings.LOG_MAX_FILE_SIZE_BYTES,
            backupCount=log_settings.LOG_BACKUP_COUNT,
            encoding="utf-8"
        )
        file_formatter = CustomFormatter(log_settings.LOG_FILE_FORMAT)
        file_handler.setFormatter(file_formatter)
        _logger.addHandler(file_handler)

        return _logger

    def update_loggers(self):
        """
        更新日志实例
        """
        _new_loggers: Dict[str, Any] = {}
        for log_file, _logger in self._loggers.items():
            # 移除已有的 handler，避免重复添加
            for handler in _logger.handlers:
                _logger.removeHandler(handler)
            # 重新设置日志实例
            _new_logger = self.__setup_logger(log_file=log_file)
            _new_loggers[log_file] = _new_logger

        self._loggers = _new_loggers

    def logger(self, method: str, msg: str, *args, **kwargs):
        """
        获取模块的logger
        :param method: 日志方法
        :param msg: 日志信息
        """
        # 获取调用者文件名和插件名
        caller_name, plugin_name = self.__get_caller()
        # 区分插件日志
        if plugin_name:
            # 使用插件日志文件
            logfile = Path("plugins") / f"{plugin_name}.log"
        else:
            # 使用默认日志文件
            logfile = self._default_log_file

        # 获取调用者的模块的logger
        _logger = self._loggers.get(logfile)
        if not _logger:
            _logger = self.__setup_logger(log_file=logfile)
            self._loggers[logfile] = _logger
        # 调用logger的方法打印日志
        if hasattr(_logger, method):
            log_method = getattr(_logger, method)
            log_method(f"{caller_name} - {msg}", *args, **kwargs)

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


# 实例化日志设置
log_settings = LogSettings()

# 初始化日志管理
logger = LoggerManager()
