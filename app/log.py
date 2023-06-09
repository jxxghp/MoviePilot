import logging
from logging.handlers import RotatingFileHandler

from app.core.config import settings

# logger
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# 创建终端输出Handler
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.DEBUG)

# 创建文件输出Handler
file_handler = RotatingFileHandler(filename=settings.LOG_PATH / 'moviepilot.log',
                                   mode='w',
                                   maxBytes=5 * 1024 * 1024,
                                   backupCount=3,
                                   encoding='utf-8')
file_handler.setLevel(logging.INFO)

# 定义日志输出格式
formatter = logging.Formatter("%(asctime)s - %(filename)s -【%(levelname)s】%(message)s")
console_handler.setFormatter(formatter)
file_handler.setFormatter(formatter)

# 将Handler添加到Logger
logger.addHandler(console_handler)
logger.addHandler(file_handler)
