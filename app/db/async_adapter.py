import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import wraps, partial
from typing import Any, Callable, Coroutine, TypeVar

from sqlalchemy.orm import Session

from app.db import ScopedSession

T = TypeVar('T')

# 全局线程池，用于执行同步数据库操作
_db_executor = ThreadPoolExecutor(max_workers=10, thread_name_prefix="db_async")


def async_db_operation(func: Callable[..., T]) -> Callable[..., Coroutine[Any, Any, T]]:
    """
    将同步数据库操作转换为异步操作的装饰器
    """

    @wraps(func)
    async def wrapper(*args, **kwargs) -> T:
        # 在线程池中执行同步数据库操作
        loop = asyncio.get_event_loop()
        partial_func = partial(func, *args, **kwargs)
        return await loop.run_in_executor(_db_executor, partial_func, ())

    return wrapper


def async_db_session(func: Callable[..., T]) -> Callable[..., Coroutine[Any, Any, T]]:
    """
    为异步操作提供数据库会话的装饰器
    """

    @wraps(func)
    async def wrapper(*args, **kwargs) -> T:
        # 创建数据库会话
        db = ScopedSession()
        try:
            # 将数据库会话添加到参数中
            if 'db' not in kwargs:
                kwargs['db'] = db
            # 在线程池中执行同步数据库操作
            loop = asyncio.get_event_loop()
            partial_func = partial(func, *args, **kwargs)
            return await loop.run_in_executor(_db_executor, partial_func, ())
        finally:
            db.close()

    return wrapper


class AsyncDbOper:
    """
    异步数据库操作基类
    """

    def __init__(self, db: Session = None):
        self._db = db

    async def _get_db(self) -> Session:
        """
        获取数据库会话
        """
        if self._db:
            return self._db
        return ScopedSession()

    @staticmethod
    async def _execute_sync(func: Callable[..., T], *args, **kwargs) -> T:
        """
        在线程池中执行同步数据库操作
        """
        loop = asyncio.get_event_loop()
        partial_func = partial(func, *args, **kwargs)
        return await loop.run_in_executor(_db_executor, partial_func, ())


def to_async_db_oper(sync_oper_class):
    """
    将同步数据库操作类转换为异步版本的装饰器
    """

    class AsyncOperClass(AsyncDbOper):
        def __init__(self, db: Session = None):
            super().__init__(db)
            self._sync_oper = sync_oper_class(db)

        def __getattr__(self, name):
            """动态获取同步操作类的方法并转换为异步"""
            if hasattr(self._sync_oper, name):
                method = getattr(self._sync_oper, name)
                if callable(method):
                    return async_db_operation(method)
            raise AttributeError(f"'{self.__class__.__name__}' object has no attribute '{name}'")

    return AsyncOperClass


# 异步数据库会话获取函数
async def get_async_db():
    """
    异步获取数据库会话
    """

    def _get_db():
        return ScopedSession()

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_db_executor, _get_db)  # type: ignore


# 异步上下文管理器
class AsyncDbSession:
    """
    异步数据库会话上下文管理器
    """

    def __init__(self):
        self.db = None

    async def __aenter__(self):
        self.db = await get_async_db()
        return self.db

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.db:
            self.db.close()


def shutdown_db_executor():
    """关闭数据库线程池"""
    global _db_executor
    if _db_executor:
        _db_executor.shutdown(wait=True)
