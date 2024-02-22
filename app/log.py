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
        console_handler.setLevel(logging.DEBUG)
        console_formatter = CustomFormatter("%(leveltext)s%(name)s - %(message)s")
        console_handler.setFormatter(console_formatter)
        _logger.addHandler(console_handler)

        # 文件日志
        file_handler = RotatingFileHandler(filename=log_file_path,
                                           mode='w',
                                           maxBytes=5 * 1024 * 1024,
                                           backupCount=3,
                                           encoding='utf-8')
        file_handler.setLevel(logging.INFO)
        file_formater = CustomFormatter("【%(levelname)s】%(asctime)s - %(name)s - %(message)s")
        file_handler.setFormatter(file_formater)
        _logger.addHandler(file_handler)

        return _logger

    def logger(self, path: str) -> logging.Logger:
        """
        获取模块的logger
        :param path: 当前运行程序路径
        """
        filepath = Path(path)

        # 区分插件日志
        if "plugins" in filepath.parts:
            # 使用插件日志文件
            plugin_name = filepath.parts[filepath.parts.index("plugins") + 1]
            logfile = Path("plugins") / f"{plugin_name}.log"
        else:
            # 使用默认日志文件
            logfile = self._default_log_file

        # 获取调用者的模块的logger
        _logger = self._loggers.get(logfile)
        if not _logger:
            _logger = self.__setup_logger(logfile)
            self._loggers[logfile] = _logger
        return _logger

    def info(self, msg, *args, **kwargs):
        """
        重载info方法，按模块区分输出
        """
        self.logger(inspect.stack()[1].filename).info(msg, *args, **kwargs)

    def debug(self, msg, *args, **kwargs):
        """
        重载debug方法，按模块区分输出
        """
        self.logger(inspect.stack()[1].filename).debug(msg, *args, **kwargs)

    def warning(self, msg, *args, **kwargs):
        """
        重载warning方法，按模块区分输出
        """
        self.logger(inspect.stack()[1].filename).warning(msg, *args, **kwargs)

    def warn(self, msg, *args, **kwargs):
        """
        重载warn方法，按模块区分输出
        """
        self.warning(msg, *args, **kwargs)

    def error(self, msg, *args, **kwargs):
        """
        重载error方法，按模块区分输出
        """
        self.logger(inspect.stack()[1].filename).error(msg, *args, **kwargs)

    def critical(self, msg, *args, **kwargs):
        """
        重载critical方法，按模块区分输出
        """
        self.logger(inspect.stack()[1].filename).critical(msg, *args, **kwargs)


# 初始化公共日志
logger = LoggerManager()
