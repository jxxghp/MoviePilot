"""订阅写入事务适配器的启动装配。"""

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.application.subscription.write import (
    AfterCommitEffect,
    AsyncAfterCommitEffect,
    AsyncCreateSubscriptionCommand,
    CreateSubscriptionCommand,
)
from app.db.oper.subscribe import SubscribeOper
from app.db.uow import SqlAlchemyAsyncUnitOfWork, SqlAlchemyUnitOfWork


class TransactionalSubscribeWriter:
    """为每次订阅新增创建独占会话，并把提交权交给 Application Command。"""

    def __init__(
        self,
        sync_session: Callable[[], Session],
        async_session: Callable[
            [],
            AbstractAsyncContextManager[AsyncSession],
        ],
    ) -> None:
        """注入同步会话工厂和异步会话作用域。"""
        self._sync_session = sync_session
        self._async_session = async_session

    def add(
        self,
        identity: dict,
        payload: dict,
        username: str | None = None,
        after_commit: AfterCommitEffect | None = None,
    ) -> tuple[int, str]:
        """在独占同步会话内执行一次完整订阅新增事务。"""
        session = self._sync_session()
        try:
            command = CreateSubscriptionCommand(
                repository=SubscribeOper(session),
                unit_of_work=SqlAlchemyUnitOfWork(session),
            )
            return command.execute(identity, payload, username, after_commit)
        finally:
            session.close()

    async def async_add(
        self,
        identity: dict,
        payload: dict,
        username: str | None = None,
        after_commit: AsyncAfterCommitEffect | None = None,
    ) -> tuple[int, str]:
        """在独占异步会话作用域内执行一次完整订阅新增事务。"""
        async with self._async_session() as session:
            command = AsyncCreateSubscriptionCommand(
                repository=SubscribeOper(session),
                unit_of_work=SqlAlchemyAsyncUnitOfWork(session),
            )
            return await command.execute(
                identity,
                payload,
                username,
                after_commit,
            )
