import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, wait
from contextvars import copy_context
from typing import Any, Callable, TypeVar, cast

from app.foundation.singleton import Singleton
from app.runtime.settings import RuntimeSettingsCompat

settings = RuntimeSettingsCompat()

_Result = TypeVar("_Result")
_THREAD_POOL_STOP_TIMEOUT_SECONDS = 10.0


class _OwnedThreadPoolExecutor(ThreadPoolExecutor):
    """
    追踪所有提交入口的共享执行器，包括旧调用方直接使用的 ``pool.submit``。
    """

    def __init__(self, max_workers: int) -> None:
        """初始化线程池及其 Future owner 集合。"""
        super().__init__(max_workers=max_workers)
        self._ownership_lock = threading.RLock()
        self._owned_futures: set[Future[Any]] = set()

    def submit(
            self,
            fn: Callable[..., _Result],
            /,
            *args: Any,
            **kwargs: Any,
    ) -> Future[_Result]:
        """提交任务并在其达到终态前保留 owner。"""
        with self._ownership_lock:
            future = super().submit(fn, *args, **kwargs)
            self._owned_futures.add(future)
            future.add_done_callback(self._discard_future)
            return future

    def _discard_future(self, future: Future[Any]) -> None:
        """任务达到终态后释放 owner 记录。"""
        with self._ownership_lock:
            self._owned_futures.discard(future)

    def shutdown_bounded(self, timeout: float) -> bool:
        """
        封口新提交并有限等待全部已接受任务。

        :param timeout: 等待 Future 达到终态的最长秒数
        :return: 所有任务与 worker 均已终止时返回 True，否则返回 False
        """
        deadline = time.monotonic() + max(0.0, timeout)
        with self._ownership_lock:
            # 不取消排队工作，保持历史 shutdown(wait=True) 的完成语义。
            super().shutdown(wait=False)
            owned_futures = tuple(self._owned_futures)
        if owned_futures:
            _, pending_futures = wait(
                owned_futures,
                timeout=max(0.0, deadline - time.monotonic()),
            )
            if pending_futures:
                return False
        # 标准库只提供无界 wait=True；Future 又会先标记完成再执行 done callback，
        # 因此封口后读取稳定 worker 集合，复用同一 deadline 做有限 join。
        worker_threads = tuple(cast(set[threading.Thread], self._threads))
        current_thread = threading.current_thread()
        for worker_thread in worker_threads:
            if worker_thread is current_thread:
                continue
            worker_thread.join(
                timeout=max(0.0, deadline - time.monotonic()),
            )
        return all(not worker_thread.is_alive() for worker_thread in worker_threads)


# strict mypy 跳过 foundation 实现导入，因此无法在本文件解析既有 Singleton 元类类型。
class ThreadHelper(metaclass=Singleton):  # type: ignore[metaclass]
    """
    共享后台线程池 owner，负责关联上下文传播和生命周期收敛。
    """

    def __init__(self) -> None:
        """按系统配置创建共享后台线程池。"""
        self.pool = _OwnedThreadPoolExecutor(max_workers=settings.CONF.threadpool)

    def submit(
            self,
            func: Callable[..., _Result],
            *args: Any,
            **kwargs: Any,
    ) -> Future[_Result]:
        """
        提交任务
        :param func: 函数
        :param args: 参数
        :param kwargs: 参数
        :return: future
        """
        context = copy_context()
        return self.pool.submit(context.run, func, *args, **kwargs)

    def shutdown(
            self,
            timeout: float = _THREAD_POOL_STOP_TIMEOUT_SECONDS,
    ) -> bool:
        """
        有限等待共享线程池关闭。

        :param timeout: 等待已接受任务达到终态的最长秒数
        :return: 全部任务和 worker 均已终止时返回 True，否则返回 False
        """
        return self.pool.shutdown_bounded(timeout=timeout)
