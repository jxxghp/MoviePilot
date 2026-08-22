"""SQLAlchemy 请求级事务适配器与旧 Oper 事务执行端口。"""

from collections.abc import Awaitable, Callable
from typing import Protocol, TypeVar

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session


T = TypeVar("T")


class SyncTransactionRunner(Protocol):
    """为无显式 Session 的兼容写入口提供独占同步事务。"""

    def __call__(self, operation: Callable[[Session], T]) -> T:
        """在一个独占会话中执行并提交操作。"""
        ...


class AsyncTransactionRunner(Protocol):
    """为无显式 Session 的兼容写入口提供独占异步事务。"""

    def __call__(
        self,
        operation: Callable[[AsyncSession], Awaitable[T]],
    ) -> Awaitable[T]:
        """在一个独占异步会话中执行并提交操作。"""
        ...


_sync_transaction_runner: SyncTransactionRunner | None = None
_async_transaction_runner: AsyncTransactionRunner | None = None


def configure_transaction_runners(
    *,
    sync: SyncTransactionRunner,
    async_: AsyncTransactionRunner,
) -> None:
    """由组合根登记旧 Oper 兼容入口使用的显式事务执行器。"""
    global _sync_transaction_runner, _async_transaction_runner
    _sync_transaction_runner = sync
    _async_transaction_runner = async_


def run_sync_transaction(operation: Callable[[Session], T]) -> T:
    """委托组合根在独占同步事务中执行兼容写操作。"""
    if _sync_transaction_runner is None:
        raise RuntimeError("同步事务执行器尚未配置")
    return _sync_transaction_runner(operation)


async def run_async_transaction(
    operation: Callable[[AsyncSession], Awaitable[T]],
) -> T:
    """委托组合根在独占异步事务中执行兼容写操作。"""
    if _async_transaction_runner is None:
        raise RuntimeError("异步事务执行器尚未配置")
    return await _async_transaction_runner(operation)


class SqlAlchemyUnitOfWork:
    """把同步 Session 的提交与回滚能力适配为应用层事务端口。"""

    def __init__(self, session: Session) -> None:
        """保存由请求依赖提供的同步数据库会话。"""
        self._session = session

    def commit(self) -> None:
        """提交请求级事务。"""
        self._session.commit()

    def rollback(self) -> None:
        """回滚请求级事务。"""
        self._session.rollback()


class SqlAlchemyAsyncUnitOfWork:
    """把 AsyncSession 的提交与回滚能力适配为应用层事务端口。"""

    def __init__(self, session: AsyncSession) -> None:
        """保存由请求依赖提供的数据库会话。"""
        self._session = session

    async def commit(self) -> None:
        """提交请求级事务。"""
        await self._session.commit()

    async def rollback(self) -> None:
        """回滚请求级事务。"""
        await self._session.rollback()
