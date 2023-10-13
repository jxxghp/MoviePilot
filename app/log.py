import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

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
        if record.filename == "__init__.py":
            record.filename = Path(record.pathname).parent.name
        return super().format(record)


# DEBUG
logger = logging.getLogger()
if settings.DEBUG:
    logger.setLevel(logging.DEBUG)
else:
    logger.setLevel(logging.INFO)

# 终端日志
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.DEBUG)
console_formatter = CustomFormatter("%(leveltext)s%(filename)s - %(message)s")
console_handler.setFormatter(console_formatter)
logger.addHandler(console_handler)

# 文件日志
file_handler = RotatingFileHandler(filename=settings.LOG_PATH / 'moviepilot.log',
                                   mode='w',
                                   maxBytes=5 * 1024 * 1024,
                                   backupCount=3,
                                   encoding='utf-8')
file_handler.setLevel(logging.INFO)
file_formater = CustomFormatter("【%(levelname)s】%(asctime)s - %(filename)s - %(message)s")
file_handler.setFormatter(file_formater)
logger.addHandler(file_handler)
