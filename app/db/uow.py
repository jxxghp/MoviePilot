"""SQLAlchemy 请求级事务适配器。"""

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session


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
