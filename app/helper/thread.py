from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from app.utils.singleton import Singleton
from app.core.config import settings


class ThreadHelper(metaclass=Singleton):
    """
    线程池管理
    """
    def __init__(self):
        self.pool = ThreadPoolExecutor(max_workers=settings.CONF['threadpool'])

    def submit(self, func, *args, **kwargs):
        """
        提交任务
        :param func: 函数
        :param args: 参数
        :param kwargs: 参数
        :return: future
        """
        return self.pool.submit(func, *args, **kwargs)

    def shutdown(self):
        """
        关闭线程池
        :return:
        """
        self.pool.shutdown()

    def __del__(self):
        self.shutdown()
