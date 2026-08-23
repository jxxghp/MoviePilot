"""订阅事务作用域及提交后回调的组合装配。"""

from contextlib import asynccontextmanager, contextmanager
from typing import Any

from app.adapters.external.server import MoviePilotServerHelper
from app.application.subscription.complete import (
    CompleteSubscriptionCommand,
    configure_subscription_completion_scope,
)
from app.application.subscription.delete import (
    DeleteSubscribeCommand,
    configure_delete_subscribe_scope,
)
from app.application.subscription.mutation import (
    SubscriptionMutationService,
    configure_subscription_mutation_scope,
)
from app.db.adapters.outbox import (
    SqlAlchemyAsyncOutboxStager,
    SqlAlchemyOutboxRepository,
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


def _publish_completed(payload: dict[str, Any]) -> None:
    """发布已提交的订阅完成事件。"""
    EventManager().send_event(EventType.SubscribeComplete, payload)


@contextmanager
def subscription_completion_scope():
    """为同步完成链创建独占 Session、UoW 与 durable outbox。"""
    session = SessionFactory()
    try:
        yield CompleteSubscriptionCommand(
            repository=SubscribeOper(session),
            unit_of_work=SqlAlchemyUnitOfWork(session),
            outbox=SqlAlchemyOutboxRepository(session),
            publish=_publish_completed,
        )
    finally:
        session.close()


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
            report_deleted=MoviePilotServerHelper.async_sub_done_durable,
            outbox=SqlAlchemyAsyncOutboxStager(session),
        )


def configure_transactional_subscription_scopes() -> None:
    """登记 Agent 等非 HTTP 入口复用的订阅事务作用域。"""
    configure_subscription_mutation_scope(subscription_mutation_scope)
    configure_delete_subscribe_scope(delete_subscribe_scope)
    configure_subscription_completion_scope(subscription_completion_scope)
