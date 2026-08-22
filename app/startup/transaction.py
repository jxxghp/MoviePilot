"""旧 Oper 写入口的 SQLAlchemy 事务执行适配器。"""

from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from typing import TypeVar

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.db.uow import SqlAlchemyAsyncUnitOfWork, SqlAlchemyUnitOfWork


T = TypeVar("T")


class TransactionalWriteRunner:
    """为兼容写入口创建独占会话，并用 UoW 明确提交或回滚。"""

    def __init__(
        self,
        *,
        sync_session: Callable[[], Session],
        async_session: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    ) -> None:
        """保存同步会话工厂和异步会话上下文工厂。"""
        self._sync_session = sync_session
        self._async_session = async_session

    def sync(self, operation: Callable[[Session], T]) -> T:
        """在独占同步 Session 中执行操作并统一收口事务。"""
        session = self._sync_session()
        # 兼容 Oper 历史上会返回刚写入的 ORM 对象；提交后若过期，Session 关闭后连主键
        # 都无法读取。独占短会话没有后续一致性读取需求，因此保留已 flush 的字段快照。
        session.expire_on_commit = False
        unit_of_work = SqlAlchemyUnitOfWork(session)
        try:
            result = operation(session)
            unit_of_work.commit()
            return result
        except Exception:
            unit_of_work.rollback()
            raise
        finally:
            session.close()

    async def async_(
        self,
        operation: Callable[[AsyncSession], Awaitable[T]],
    ) -> T:
        """在独占 AsyncSession 中执行操作并统一收口事务。"""
        async with self._async_session() as session:
            # 与同步兼容入口保持相同的返回对象生命周期。
            session.sync_session.expire_on_commit = False
            unit_of_work = SqlAlchemyAsyncUnitOfWork(session)
            try:
                result = await operation(session)
                await unit_of_work.commit()
                return result
            except Exception:
                await unit_of_work.rollback()
                raise
