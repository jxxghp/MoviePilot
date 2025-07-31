import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Coroutine, Any, TypeVar

T = TypeVar('T')


class AsyncUtils:
    """
    异步工具类，用于在同步环境中调用异步方法
    """

    @staticmethod
    def run_async(coro: Coroutine[Any, Any, T]) -> T:
        """
        在同步环境中安全地执行异步协程
        
        :param coro: 要执行的协程
        :return: 协程的返回值
        :raises: 协程执行过程中的任何异常
        """
        try:
            # 尝试获取当前运行的事件循环
            asyncio.get_running_loop()
            # 如果有运行中的事件循环，在新线程中执行
            return AsyncUtils._run_in_thread(coro)
        except RuntimeError:
            # 没有运行中的事件循环，直接使用 asyncio.run
            return asyncio.run(coro)

    @staticmethod
    def _run_in_thread(coro: Coroutine[Any, Any, T]) -> T:
        """
        在新线程中创建事件循环并执行协程
        
        :param coro: 要执行的协程
        :return: 协程的返回值
        """
        result = None
        exception = None

        def _run():
            nonlocal result, exception
            try:
                # 在新线程中创建新的事件循环
                new_loop = asyncio.new_event_loop()
                asyncio.set_event_loop(new_loop)
                try:
                    result = new_loop.run_until_complete(coro)
                finally:
                    new_loop.close()
            except Exception as e:
                exception = e

        # 在新线程中执行
        thread = threading.Thread(target=_run)
        thread.start()
        thread.join()

        if exception:
            raise exception

        return result

    @staticmethod
    def run_async_in_executor(coro: Coroutine[Any, Any, T]) -> T:
        """
        使用线程池执行器在新线程中运行异步协程
        
        :param coro: 要执行的协程
        :return: 协程的返回值
        """
        try:
            # 检查是否有运行中的事件循环
            asyncio.get_running_loop()
            # 有运行中的事件循环，使用线程池
            with ThreadPoolExecutor() as executor:
                future = executor.submit(asyncio.run, coro)
                return future.result()
        except RuntimeError:
            # 没有运行中的事件循环，直接运行
            return asyncio.run(coro)
