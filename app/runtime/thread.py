from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context

from app.runtime.config import settings
from app.foundation.singleton import Singleton


class ThreadHelper(metaclass=Singleton):
    """
    线程池管理
    """
    def __init__(self):
        """按系统配置创建共享后台线程池。"""
        self.pool = ThreadPoolExecutor(max_workers=settings.CONF.threadpool)

    def submit(self, func, *args, **kwargs):
        """
        提交任务
        :param func: 函数
        :param args: 参数
        :param kwargs: 参数
        :return: future
        """
        context = copy_context()
        return self.pool.submit(context.run, func, *args, **kwargs)

    def shutdown(self):
        """
        关闭线程池
        :return:
        """
        self.pool.shutdown()
