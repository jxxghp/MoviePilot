"""
受控线程池提交与关闭，保留任务上下文并回收线程执行资源。
"""

import threading
from concurrent.futures import Future
from contextvars import copy_context
from typing import Any, Callable, TypeVar, cast

from app.foundation.singleton import Singleton
from app.runtime.execution import OwnedThreadPoolExecutor
from app.runtime.settings import get_runtime_setting

_Result = TypeVar("_Result")
_THREAD_POOL_STOP_TIMEOUT_SECONDS = 10.0

# 阶段 64 曾在本模块引入该私有名；保留精确别名，避免测试或外部诊断代码失效。
_OwnedThreadPoolExecutor = OwnedThreadPoolExecutor


class ThreadHelper(metaclass=Singleton):
    """
    共享后台线程池 owner，负责关联上下文传播和生命周期收敛。
    """

    def __init__(self) -> None:
        """
        按系统配置创建共享后台线程池。
        """
        self._lifecycle_lock = threading.RLock()
        self.pool = self._new_pool()

    @staticmethod
    def _new_pool() -> OwnedThreadPoolExecutor:
        """
        按当前运行配置构造一个新的共享线程池 owner。
        """
        return OwnedThreadPoolExecutor(
            max_workers=get_runtime_setting('CONF').threadpool
        )

    def submit(
            self,
            func: Callable[..., _Result],
            *args: Any,
            **kwargs: Any,
    ) -> Future[_Result]:
        """
        向共享后台线程池提交任务，并传播提交时捕获的上下文。

        :param func: 需要在线程池中执行的同步函数
        :param args: 传给目标函数的位置参数
        :param kwargs: 传给目标函数的关键字参数
        :return: 可等待执行结果或取消尚未开始工作的 Future
        """
        context = copy_context()
        with self._lifecycle_lock:
            # mypy 严格检查跳过 execution 实现导入，需要在兼容门面恢复 Future 泛型。
            return cast(
                Future[_Result],
                self.pool.submit(context.run, func, *args, **kwargs),
            )

    def reopen(self) -> bool:
        """
        为新的应用生命周期恢复线程池提交准入。

        只有上一 owner 已完全收敛时才会构造新线程池，避免跨 lifespan
        同时保留两组 worker。
        """
        with self._lifecycle_lock:
            if self.pool.accepting:
                return True
            if not self.pool.shutdown_bounded(timeout=0.0):
                return False
            self.pool = self._new_pool()
            return True

    def shutdown(
            self,
            timeout: float = _THREAD_POOL_STOP_TIMEOUT_SECONDS,
    ) -> bool:
        """
        有限等待共享线程池关闭。

        :param timeout: 等待已接受任务达到终态的最长秒数
        :return: 全部任务和 worker 均已终止时返回 True，否则返回 False
        """
        with self._lifecycle_lock:
            return bool(self.pool.shutdown_bounded(timeout=timeout))
