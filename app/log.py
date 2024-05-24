import inspect
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Dict, Any

import click

from app.core.config import settings

# 日志级别颜色
level_name_colors = {
    logging.DEBUG: lambda level_name: click.style(str(level_name), fg="cyan"),
    logging.INFO: lambda level_name: click.style(str(level_name), fg="green"),
    logging.WARNING: lambda level_name: click.style(str(level_name), fg="yellow"),
    logging.ERROR: lambda level_name: click.style(str(level_name), fg="red"),
    logging.CRITICAL: lambda level_name: click.style(
        str(level_name), fg="bright_red"
    ),
}


class CustomFormatter(logging.Formatter):
    """
    定义日志输出格式
    """

    def format(self, record):
        seperator = " " * (8 - len(record.levelname))
        record.leveltext = level_name_colors[record.levelno](record.levelname + ":") + seperator
        return super().format(record)


class LoggerManager:
    """
    日志管理
    """
    # 管理所有的Logger
    _loggers: Dict[str, Any] = {}
    # 默认日志文件
    _default_log_file = "moviepilot.log"

    @staticmethod
    def __get_caller():
        """
        获取调用者的文件名称与插件名称(如果是插件调用内置的模块, 也能写入到插件日志文件中)
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
        log_file：日志文件相对路径
        """
        log_file_path = settings.LOG_PATH / log_file
        if not log_file_path.parent.exists():
            log_file_path.parent.mkdir(parents=True, exist_ok=True)

        # 创建新实例
        _logger = logging.getLogger(log_file_path.stem)

        # DEBUG
        if settings.DEBUG:
            _logger.setLevel(logging.DEBUG)
        else:
            _logger.setLevel(logging.INFO)

        # 移除已有的 handler，避免重复添加
        for handler in _logger.handlers:
            _logger.removeHandler(handler)

        # 终端日志
        console_handler = logging.StreamHandler()
        console_formatter = CustomFormatter(f"%(leveltext)s%(message)s")
        console_handler.setFormatter(console_formatter)
        _logger.addHandler(console_handler)

        # 文件日志
        file_handler = RotatingFileHandler(filename=log_file_path,
                                           mode='w',
                                           maxBytes=5 * 1024 * 1024,
                                           backupCount=3,
                                           encoding='utf-8')
        file_formater = CustomFormatter(f"【%(levelname)s】%(asctime)s - %(message)s")
        file_handler.setFormatter(file_formater)
        _logger.addHandler(file_handler)

        return _logger

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
            _logger = self.__setup_logger(logfile)
            self._loggers[logfile] = _logger
        # 调用logger的方法打印日志
        if hasattr(_logger, method):
            method = getattr(_logger, method)
            method(f"{caller_name} - {msg}", *args, **kwargs)

    def info(self, msg: str, *args, **kwargs):
        """
        重载info方法
        """
        self.logger("info", msg, *args, **kwargs)

    def debug(self, msg: str, *args, **kwargs):
        """
        重载debug方法
        """
        self.logger("debug", msg, *args, **kwargs)

    def warning(self, msg: str, *args, **kwargs):
        """
        重载warning方法
        """
        self.logger("warning", msg, *args, **kwargs)

    def warn(self, msg: str, *args, **kwargs):
        """
        重载warn方法
        """
        self.logger("warning", msg, *args, **kwargs)

    def error(self, msg: str, *args, **kwargs):
        """
        重载error方法
        """
        self.logger("error", msg, *args, **kwargs)

    def critical(self, msg: str, *args, **kwargs):
        """
        重载critical方法
        """
        self.logger("critical", msg, *args, **kwargs)


# 初始化公共日志
logger = LoggerManager()
