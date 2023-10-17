from concurrent.futures import ThreadPoolExecutor

from app.utils.singleton import Singleton


class ThreadHelper(metaclass=Singleton):
    """
    线程池管理
    """
    def __init__(self, max_workers=50):
        self.pool = ThreadPoolExecutor(max_workers=max_workers)

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
