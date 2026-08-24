import asyncio
import inspect
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, wait
from contextvars import copy_context
from functools import partial, wraps
from typing import Any, Callable, TypeVar, cast

from app.schemas.exception import ImmediateException
from anyio.to_thread import run_sync


TaskResult = TypeVar("TaskResult")
ExecutorResult = TypeVar("ExecutorResult")


class OwnedThreadPoolExecutor(ThreadPoolExecutor):
    """
    追踪已接受 Future，并提供可重试的有界关闭合同。

    该 owner 不取消排队任务，保持 ``ThreadPoolExecutor.shutdown(wait=True)``
    的历史完成语义；区别仅在于调用方可以在预算耗尽后保留同一实例继续收敛。
    """

    def __init__(self, max_workers: int | None = None) -> None:
        """初始化线程池、提交准入状态和 Future owner 集合。"""
        super().__init__(max_workers=max_workers)
        self._ownership_lock = threading.RLock()
        self._accepting = True
        self._owned_futures: set[Future[Any]] = set()

    @property
    def accepting(self) -> bool:
        """返回执行器是否仍允许提交新任务。"""
        with self._ownership_lock:
            return self._accepting

    def submit(
            self,
            fn: Callable[..., ExecutorResult],
            /,
            *args: Any,
            **kwargs: Any,
    ) -> Future[ExecutorResult]:
        """提交任务并在其达到终态前保留 owner。"""
        with self._ownership_lock:
            future = super().submit(fn, *args, **kwargs)
            self._owned_futures.add(future)
            future.add_done_callback(self._discard_future)
            return future

    def shutdown(
            self,
            wait: bool = True,
            *,
            cancel_futures: bool = False,
    ) -> None:
        """封口提交准入并保留标准库 shutdown 的调用语义。"""
        with self._ownership_lock:
            self._accepting = False
            # 先在锁内封口；真正等待必须在锁外进行，否则 worker 的完成回调无法释放 owner。
            super().shutdown(wait=False, cancel_futures=cancel_futures)
        if wait:
            super().shutdown(wait=True, cancel_futures=cancel_futures)

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
            self._accepting = False
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


async def await_task_to_terminal(
    task: asyncio.Future[TaskResult],
) -> TaskResult:
    """忽略当前调用方的重复取消，直到受保护任务进入真实终态。"""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
        except BaseException:
            break
    return task.result()


async def run_in_threadpool(
    func: Callable[..., Any],
    *args: Any,
    **kwargs: Any,
) -> Any:
    """在线程中执行同步函数，保持 FastAPI 旧帮助函数的参数语义。"""
    if kwargs:
        func = partial(func, **kwargs)
    context = copy_context()
    return await run_sync(context.run, func, *args)


async def run_in_threadpool_to_completion(
    func: Callable[..., Any],
    *args: Any,
    **kwargs: Any,
) -> Any:
    """在线程调用取得终态后传播取消，避免提前释放仍在使用的执行容量。"""
    worker_task = asyncio.create_task(run_in_threadpool(func, *args, **kwargs))
    cancellation: asyncio.CancelledError | None = None
    while not worker_task.done():
        try:
            await asyncio.wait({worker_task})
        except asyncio.CancelledError as error:
            cancellation = cancellation or error
            continue
    try:
        result = worker_task.result()
    except Exception as error:
        if cancellation is not None:
            raise cancellation from error
        raise
    if cancellation is not None:
        raise cancellation
    return result


def retry(ExceptionToCheck: Any,
          tries: int = 3, delay: int = 3, backoff: int = 2, logger: Any = None):
    """
    :param ExceptionToCheck: 需要捕获的异常
    :param tries: 重试次数
    :param delay: 延迟时间
    :param backoff: 延迟倍数
    :param logger: 日志对象
    """

    def deco_retry(f):
        def f_retry(*args, **kwargs):
            mtries, mdelay = tries, delay
            while mtries > 1:
                try:
                    return f(*args, **kwargs)
                except ImmediateException:
                    raise
                except ExceptionToCheck as e:
                    msg = f"{str(e)}, {mdelay} 秒后重试 ..."
                    if logger:
                        logger.warn(msg)
                    else:
                        print(msg)
                    time.sleep(mdelay)
                    mtries -= 1
                    mdelay *= backoff
            return f(*args, **kwargs)

        async def async_f_retry(*args, **kwargs):
            mtries, mdelay = tries, delay
            while mtries > 1:
                try:
                    return await f(*args, **kwargs)
                except ImmediateException:
                    raise
                except ExceptionToCheck as e:
                    msg = f"{str(e)}, {mdelay} 秒后重试 ..."
                    if logger:
                        logger.warn(msg)
                    else:
                        print(msg)
                    await asyncio.sleep(mdelay)
                    mtries -= 1
                    mdelay *= backoff
            return await f(*args, **kwargs)

        # 根据函数类型返回相应的包装器
        if inspect.iscoroutinefunction(f):
            return async_f_retry
        else:
            return f_retry

    return deco_retry


def log_execution_time(logger: Any = None):
    """
    记录函数执行时间的装饰器
    :param logger: 日志记录器对象，用于记录异常信息
    """

    def decorator(func: Callable):
        @wraps(func)
        def wrapper(*args, **kwargs):
            start_time = time.time()
            result = func(*args, **kwargs)
            end_time = time.time()
            msg = f"{func.__name__} execution time: {end_time - start_time:.2f} seconds"
            if logger:
                logger.debug(msg)
            else:
                print(msg)
            return result

        @wraps(func)
        async def async_wrapper(*args, **kwargs):
            start_time = time.time()
            result = await func(*args, **kwargs)
            end_time = time.time()
            msg = f"{func.__name__} execution time: {end_time - start_time:.2f} seconds"
            if logger:
                logger.debug(msg)
            else:
                print(msg)
            return result

        # 根据函数类型返回相应的包装器
        if inspect.iscoroutinefunction(func):
            return async_wrapper
        else:
            return wrapper

    return decorator
