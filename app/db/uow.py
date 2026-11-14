"""
SQLAlchemy 请求级事务适配器与无会话 Oper 事务执行端口。
"""

from collections.abc import Awaitable, Callable
from contextlib import contextmanager
from typing import Iterator, Protocol, TypeVar

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

T = TypeVar("T")


class SyncTransactionRunner(Protocol):
    """
    为无显式 Session 的 Oper 入口提供独占同步事务。
    """

    def __call__(self, operation: Callable[[Session], T]) -> T:
        """
        在独占会话中执行并提交同步数据库操作。

        :param operation: 接收当前事务 Session 的回调，不得将会话交给其他线程并发使用
        :return: 提交成功后的回调结果；回调或提交异常向上传播，实现方负责回滚并关闭会话
        """
        ...


class AsyncTransactionRunner(Protocol):
    """
    为无显式 Session 的 Oper 入口提供独占异步事务。
    """

    def __call__(
        self,
        operation: Callable[[AsyncSession], Awaitable[T]],
    ) -> Awaitable[T]:
        """
        在独占异步会话中执行并提交数据库操作。

        :param operation: 接收当前事务 AsyncSession 的异步回调，不得跨任务并发共用会话
        :return: 等待后得到提交成功的回调结果；异常向上传播，实现方负责回滚并关闭会话
        """
        ...


_sync_transaction_runner: SyncTransactionRunner | None = None
_async_transaction_runner: AsyncTransactionRunner | None = None


def configure_transaction_runners(
    *,
    sync: SyncTransactionRunner,
    async_: AsyncTransactionRunner,
) -> None:
    """
    由组合根登记无会话 Oper 入口使用的显式事务执行器。
    """
    global _sync_transaction_runner, _async_transaction_runner
    _sync_transaction_runner = sync
    _async_transaction_runner = async_


def reset_transaction_runners() -> None:
    """
    清除当前 lifespan 的无会话事务执行器，禁止关停后继续写库。
    """
    global _sync_transaction_runner, _async_transaction_runner
    _sync_transaction_runner = None
    _async_transaction_runner = None


@contextmanager
def transaction_runner_scope(
    *, sync: SyncTransactionRunner, async_: AsyncTransactionRunner,
) -> Iterator[None]:
    """
    为独立初始化命令登记事务执行器，并在退出时恢复进入前的端口。

    :param sync: 当前命令使用的独占同步事务执行器
    :param async_: 当前命令使用的独占异步事务执行器
    :return: 退出后恢复原登记状态的同步上下文
    """
    global _sync_transaction_runner, _async_transaction_runner
    previous = (_sync_transaction_runner, _async_transaction_runner)
    configure_transaction_runners(sync=sync, async_=async_)
    try:
        yield
    finally:
        _sync_transaction_runner, _async_transaction_runner = previous


def run_sync_transaction(operation: Callable[[Session], T]) -> T:
    """
    委托组合根在独占同步事务中执行 Oper 操作。

    :param operation: 使用执行器提供的 Session 完成查询或暂存修改的回调
    :return: 执行器提交后的回调结果
    :raises RuntimeError: 当前生命周期尚未配置同步事务执行器
    """
    if _sync_transaction_runner is None:
        raise RuntimeError("同步事务执行器尚未配置")
    return _sync_transaction_runner(operation)


async def run_async_transaction(
    operation: Callable[[AsyncSession], Awaitable[T]],
) -> T:
    """
    委托组合根在独占异步事务中执行 Oper 操作。

    :param operation: 使用执行器提供的 AsyncSession 完成查询或暂存修改的异步回调
    :return: 等待执行器提交后的回调结果
    :raises RuntimeError: 当前生命周期尚未配置异步事务执行器
    """
    if _async_transaction_runner is None:
        raise RuntimeError("异步事务执行器尚未配置")
    return await _async_transaction_runner(operation)


class SqlAlchemyUnitOfWork:
    """
    把同步 Session 的提交与回滚能力适配为应用层事务端口。
    """

    def __init__(self, session: Session) -> None:
        """
        保存由请求依赖提供的同步数据库会话。
        """
        self._session = session

    def commit(self) -> None:
        """
        提交请求级事务。
        """
        self._session.commit()

    def rollback(self) -> None:
        """
        回滚请求级事务。
        """
        self._session.rollback()


class SqlAlchemyAsyncUnitOfWork:
    """
    把 AsyncSession 的提交与回滚能力适配为应用层事务端口。
    """

    def __init__(self, session: AsyncSession) -> None:
        """
        保存由请求依赖提供的数据库会话。
        """
        self._session = session

    async def commit(self) -> None:
        """
        提交请求级事务。
        """
        await self._session.commit()

    async def rollback(self) -> None:
        """
        回滚请求级事务。
        """
        await self._session.rollback()
