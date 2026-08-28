"""订阅事务作用域及提交后回调的组合装配。"""

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from typing import Any

from app.adapters.external.server import MoviePilotServerHelper
from app.application.subscription.complete import (
    CompleteSubscriptionCommand,
    configure_subscription_completion_scope,
)
from app.application.subscription.delete import (
    DeleteSubscribeCommand,
    SyncDeleteSubscribeCommand,
    configure_delete_subscribe_scope,
    configure_sync_delete_subscribe_scope,
)
from app.application.subscription.mutation import (
    SubscriptionMutationService,
    configure_subscription_mutation_scope,
)
from app.db.adapters.outbox import (
    SqlAlchemyAsyncOutboxDispatchStore,
    SqlAlchemyAsyncOutboxStager,
    SqlAlchemyOutboxDispatchStore,
    SqlAlchemyOutboxStager,
)
from app.db.oper.subscribe import SubscribeOper
from app.db.oper.subscribehistory import SubscribeHistoryOper
from app.db.session import SessionFactory, async_session_scope
from app.db.uow import SqlAlchemyAsyncUnitOfWork, SqlAlchemyUnitOfWork
from app.runtime.events import EventManager
from app.schemas.types import EventType


async def _publish_modified(payload: dict[str, Any]) -> None:
    """发布事务已提交的订阅修改事件。"""
    await EventManager().async_send_event(EventType.SubscribeModified, payload)


async def _publish_deleted(payload: dict[str, Any]) -> None:
    """发布事务已提交的订阅删除事件。"""
    await EventManager().async_send_event(EventType.SubscribeDeleted, payload)


def _publish_deleted_sync(payload: dict[str, Any]) -> None:
    """为同步消息入口发布事务已提交的订阅删除事件。"""
    EventManager().send_event(EventType.SubscribeDeleted, payload)


def _publish_completed(payload: dict[str, Any]) -> None:
    """发布已提交的订阅完成事件。"""
    EventManager().send_event(EventType.SubscribeComplete, payload)


@contextmanager
def subscription_completion_scope() -> Iterator[CompleteSubscriptionCommand]:
    """为同步完成链创建独占 Session、UoW 与 durable outbox。"""
    session = SessionFactory()
    try:
        yield CompleteSubscriptionCommand(
            repository=SubscribeOper(session),
            unit_of_work=SqlAlchemyUnitOfWork(session),
            outbox=SqlAlchemyOutboxStager(session),
            dispatch_store=SqlAlchemyOutboxDispatchStore(SessionFactory),
            publish=_publish_completed,
        )
    finally:
        session.close()


@asynccontextmanager
async def subscription_mutation_scope() -> AsyncIterator[SubscriptionMutationService]:
    """为非 HTTP 入口创建独占订阅修改会话、UoW 与 outbox。"""
    async with async_session_scope() as session:
        yield SubscriptionMutationService(
            repository=SubscribeOper(session),
            history_repository=SubscribeHistoryOper(session),
            unit_of_work=SqlAlchemyAsyncUnitOfWork(session),
            outbox=SqlAlchemyAsyncOutboxStager(session),
            dispatch_store=SqlAlchemyAsyncOutboxDispatchStore(
                async_session_scope
            ),
            publish_modified=_publish_modified,
        )


@asynccontextmanager
async def delete_subscribe_scope() -> AsyncIterator[DeleteSubscribeCommand]:
    """为非 HTTP 入口创建独占订阅删除会话、UoW 与 outbox。"""
    async with async_session_scope() as session:
        yield DeleteSubscribeCommand(
            repository=SubscribeOper(session),
            unit_of_work=SqlAlchemyAsyncUnitOfWork(session),
            publish_deleted=_publish_deleted,
            report_deleted=MoviePilotServerHelper.async_sub_done_durable,
            outbox=SqlAlchemyAsyncOutboxStager(session),
            dispatch_store=SqlAlchemyAsyncOutboxDispatchStore(
                async_session_scope
            ),
        )


@contextmanager
def sync_delete_subscribe_scope() -> Iterator[SyncDeleteSubscribeCommand]:
    """为同步消息入口创建独占 Session、UoW 与 durable outbox。"""
    session = SessionFactory()
    try:
        yield SyncDeleteSubscribeCommand(
            repository=SubscribeOper(session),
            unit_of_work=SqlAlchemyUnitOfWork(session),
            publish_deleted=_publish_deleted_sync,
            report_deleted=MoviePilotServerHelper.sub_done_durable,
            outbox=SqlAlchemyOutboxStager(session),
            dispatch_store=SqlAlchemyOutboxDispatchStore(SessionFactory),
        )
    finally:
        session.close()


def configure_transactional_subscription_scopes() -> None:
    """登记 Agent 等非 HTTP 入口复用的订阅事务作用域。"""
    configure_subscription_mutation_scope(subscription_mutation_scope)
    configure_delete_subscribe_scope(delete_subscribe_scope)
    configure_sync_delete_subscribe_scope(sync_delete_subscribe_scope)
    configure_subscription_completion_scope(subscription_completion_scope)
