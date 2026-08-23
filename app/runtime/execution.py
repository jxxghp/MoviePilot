import asyncio
import inspect
import time
from contextvars import copy_context
from functools import partial, wraps
from typing import Any, Callable, TypeVar

from app.schemas.exception import ImmediateException
from anyio.to_thread import run_sync


TaskResult = TypeVar("TaskResult")


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
