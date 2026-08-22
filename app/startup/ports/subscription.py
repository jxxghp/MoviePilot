"""订阅写入事务适配器的启动装配。"""

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.application.subscription.write import (
    AfterCommitEffect,
    AsyncAfterCommitEffect,
    AsyncCreateSubscriptionCommand,
    CreateSubscriptionCommand,
    subscription_added_event_key,
)
from app.application.subscription.delete import (
    DeleteSubscribeCommand,
    configure_delete_subscribe_scope,
)
from app.application.subscription.mutation import (
    SubscriptionMutationService,
    configure_subscription_mutation_scope,
)
from app.adapters.external.server import MoviePilotServerHelper
from app.db.oper.subscribe import SubscribeOper
from app.db.oper.subscribehistory import SubscribeHistoryOper
from app.db.session import async_session_scope
from app.db.uow import SqlAlchemyAsyncUnitOfWork, SqlAlchemyUnitOfWork
from app.startup.ports.outbox import (
    SqlAlchemyAsyncOutboxStager,
    SqlAlchemyOutboxRepository,
)
from app.runtime.events import EventManager
from app.schemas.types import EventType


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
            outbox = SqlAlchemyOutboxRepository(session)
            command = CreateSubscriptionCommand(
                repository=SubscribeOper(session),
                unit_of_work=SqlAlchemyUnitOfWork(session),
                outbox=outbox,
            )

            def delivered(subscribe_id: int) -> None:
                """执行旧 post-commit 编排，全部成功后收口 durable intent。"""
                if after_commit:
                    after_commit(subscribe_id)
                    outbox.complete_by_event_key(
                        subscription_added_event_key(subscribe_id, payload),
                        datetime.now(timezone.utc),
                    )

            return command.execute(identity, payload, username, delivered)
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
            outbox = SqlAlchemyAsyncOutboxStager(session)
            command = AsyncCreateSubscriptionCommand(
                repository=SubscribeOper(session),
                unit_of_work=SqlAlchemyAsyncUnitOfWork(session),
                outbox=outbox,
            )

            async def delivered(subscribe_id: int) -> None:
                """异步执行旧编排，全部成功后收口 durable intent。"""
                if after_commit:
                    await after_commit(subscribe_id)
                    await outbox.complete_by_event_key(
                        subscription_added_event_key(subscribe_id, payload),
                        datetime.now(timezone.utc),
                    )

            return await command.execute(
                identity,
                payload,
                username,
                delivered,
            )


async def _publish_modified(payload: dict[str, Any]) -> None:
    """发布事务已提交的订阅修改事件。"""
    await EventManager().async_send_event(EventType.SubscribeModified, payload)


async def _publish_deleted(payload: dict[str, Any]) -> None:
    """发布事务已提交的订阅删除事件。"""
    await EventManager().async_send_event(EventType.SubscribeDeleted, payload)


@asynccontextmanager
async def subscription_mutation_scope():
    """为非 HTTP 入口创建独占订阅修改会话、UoW 与 outbox。"""
    async with async_session_scope() as session:
        yield SubscriptionMutationService(
            repository=SubscribeOper(session),
            history_repository=SubscribeHistoryOper(session),
            unit_of_work=SqlAlchemyAsyncUnitOfWork(session),
            outbox=SqlAlchemyAsyncOutboxStager(session),
            publish_modified=_publish_modified,
        )


@asynccontextmanager
async def delete_subscribe_scope():
    """为非 HTTP 入口创建独占订阅删除会话、UoW 与 outbox。"""
    async with async_session_scope() as session:
        yield DeleteSubscribeCommand(
            repository=SubscribeOper(session),
            unit_of_work=SqlAlchemyAsyncUnitOfWork(session),
            publish_deleted=_publish_deleted,
            report_deleted=MoviePilotServerHelper.sub_done_async,
            outbox=SqlAlchemyAsyncOutboxStager(session),
        )


def configure_transactional_subscription_scopes() -> None:
    """登记 Agent 等非 HTTP 入口复用的订阅事务作用域。"""
    configure_subscription_mutation_scope(subscription_mutation_scope)
    configure_delete_subscribe_scope(delete_subscribe_scope)
