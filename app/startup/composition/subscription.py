"""订阅事务作用域及提交后回调的组合装配。"""

import asyncio
import threading
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from contextlib import asynccontextmanager, contextmanager
from typing import Any

from app.adapters.external.server import MoviePilotServerHelper
from app.application.outbox import AsyncOutboxDispatchStore
from app.application.rules import (
    AsyncRuleGroupMutationService,
    SyncRuleGroupMutationService,
)
from app.application.site.mutation import SyncSiteReferenceMutationService
from app.application.subscription.complete import CompleteSubscriptionCommand
from app.application.subscription.contract import SubscriptionStagingPort
from app.application.subscription.delete import (
    DeleteSubscribeCommand,
    SyncDeleteSubscribeCommand,
)
from app.application.subscription.mutation import (
    SubscriptionMutationService,
    SyncSubscriptionMutationService,
)
from app.application.subscription.write import (
    AsyncSubscriptionOutboxStager,
    AsyncUnitOfWork,
    SubscriptionBatchWritePort,
)
from app.db.adapters.configuration import SessionSystemConfigurationRepository
from app.db.adapters.outbox import (
    SqlAlchemyAsyncOutboxDispatchStore,
    SqlAlchemyAsyncOutboxStager,
    SqlAlchemyOutboxDispatchStore,
    SqlAlchemyOutboxStager,
)
from app.db.adapters.subscription import (
    SessionSubscriptionBatchWriter,
    SessionSubscriptionHistoryRepository,
    SessionSubscriptionRepository,
)
from app.db.session import SessionFactory, async_session_scope
from app.db.uow import SqlAlchemyAsyncUnitOfWork, SqlAlchemyUnitOfWork
from app.runtime.events import eventmanager
from app.schemas.common import JsonData
from app.schemas.types import EventType, SystemConfigKey

_reference_mutation_lock = threading.Lock()
SystemConfigPublisher = Callable[[Mapping[SystemConfigKey, JsonData]], None]


def build_subscription_batch_writer(
    *,
    repository: SubscriptionStagingPort,
    unit_of_work: AsyncUnitOfWork,
    outbox: AsyncSubscriptionOutboxStager,
    dispatch_store: AsyncOutboxDispatchStore,
) -> SubscriptionBatchWritePort:
    """组装复用请求级事务且在提交后结算 durable intents 的批量写端口。"""
    return SessionSubscriptionBatchWriter(
        repository=repository,
        unit_of_work=unit_of_work,
        outbox=outbox,
        dispatch_store=dispatch_store,
    )


@contextmanager
def rule_group_mutation_scope(
    publish: SystemConfigPublisher,
) -> Iterator[SyncRuleGroupMutationService]:
    """串行构造共享同步 Session 的规则定义与引用原子服务。"""
    _reference_mutation_lock.acquire()
    session = SessionFactory()
    try:
        yield SyncRuleGroupMutationService(
            configuration=SessionSystemConfigurationRepository(session),
            subscriptions=SessionSubscriptionRepository(session),
            unit_of_work=SqlAlchemyUnitOfWork(session),
            publish=publish,
        )
    finally:
        session.close()
        _reference_mutation_lock.release()


@asynccontextmanager
async def async_rule_group_mutation_scope(
    publish: SystemConfigPublisher,
) -> AsyncIterator[AsyncRuleGroupMutationService]:
    """非阻塞等待跨入口锁，并构造共享 AsyncSession 的规则原子服务。"""
    await asyncio.to_thread(_reference_mutation_lock.acquire)
    try:
        async with async_session_scope() as session:
            async def publish_async(
                values: Mapping[SystemConfigKey, JsonData],
            ) -> None:
                """适配同步快照发布器到异步命令提交后合同。"""
                publish(values)

            yield AsyncRuleGroupMutationService(
                configuration=SessionSystemConfigurationRepository(session),
                subscriptions=SessionSubscriptionRepository(session),
                unit_of_work=SqlAlchemyAsyncUnitOfWork(session),
                publish=publish_async,
            )
    finally:
        _reference_mutation_lock.release()


@contextmanager
def site_reference_mutation_scope(
    publish: SystemConfigPublisher,
) -> Iterator[SyncSiteReferenceMutationService]:
    """构造共享同步 Session 的 RSS 与订阅站点引用原子服务。"""
    _reference_mutation_lock.acquire()
    session = SessionFactory()
    try:
        yield SyncSiteReferenceMutationService(
            configuration=SessionSystemConfigurationRepository(session),
            subscriptions=SessionSubscriptionRepository(session),
            unit_of_work=SqlAlchemyUnitOfWork(session),
            publish=publish,
        )
    finally:
        session.close()
        _reference_mutation_lock.release()


async def _publish_modified(payload: dict[str, Any]) -> None:
    """发布事务已提交的订阅修改事件。"""
    await eventmanager.async_send_event(EventType.SubscribeModified, payload)


def _publish_modified_sync(payload: dict[str, Any]) -> None:
    """为同步 Chain 入口发布事务已提交的订阅修改事件。"""
    eventmanager.send_event(EventType.SubscribeModified, payload)


async def _publish_deleted(payload: dict[str, Any]) -> None:
    """发布事务已提交的订阅删除事件。"""
    await eventmanager.async_send_event(EventType.SubscribeDeleted, payload)


def _publish_deleted_sync(payload: dict[str, Any]) -> None:
    """为同步消息入口发布事务已提交的订阅删除事件。"""
    eventmanager.send_event(EventType.SubscribeDeleted, payload)


def _publish_completed(payload: dict[str, Any]) -> None:
    """发布已提交的订阅完成事件。"""
    eventmanager.send_event(EventType.SubscribeComplete, payload)


@contextmanager
def subscription_completion_scope() -> Iterator[CompleteSubscriptionCommand]:
    """为同步完成链创建独占 Session、UoW 与 durable outbox。"""
    session = SessionFactory()
    try:
        yield CompleteSubscriptionCommand(
            repository=SessionSubscriptionRepository(session),
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
            repository=SessionSubscriptionRepository(session),
            history_repository=SessionSubscriptionHistoryRepository(session),
            unit_of_work=SqlAlchemyAsyncUnitOfWork(session),
            outbox=SqlAlchemyAsyncOutboxStager(session),
            dispatch_store=SqlAlchemyAsyncOutboxDispatchStore(
                async_session_scope
            ),
            publish_modified=_publish_modified,
        )


@contextmanager
def sync_subscription_mutation_scope() -> Iterator[SyncSubscriptionMutationService]:
    """为同步 Chain 入口创建独占 Session、UoW 与 durable outbox。"""
    session = SessionFactory()
    try:
        yield SyncSubscriptionMutationService(
            repository=SessionSubscriptionRepository(session),
            unit_of_work=SqlAlchemyUnitOfWork(session),
            outbox=SqlAlchemyOutboxStager(session),
            dispatch_store=SqlAlchemyOutboxDispatchStore(SessionFactory),
            publish_modified=_publish_modified_sync,
        )
    finally:
        session.close()


@asynccontextmanager
async def delete_subscribe_scope() -> AsyncIterator[DeleteSubscribeCommand]:
    """为非 HTTP 入口创建独占订阅删除会话、UoW 与 outbox。"""
    async with async_session_scope() as session:
        yield DeleteSubscribeCommand(
            repository=SessionSubscriptionRepository(session),
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
            repository=SessionSubscriptionRepository(session),
            unit_of_work=SqlAlchemyUnitOfWork(session),
            publish_deleted=_publish_deleted_sync,
            report_deleted=MoviePilotServerHelper.sub_done_durable,
            outbox=SqlAlchemyOutboxStager(session),
            dispatch_store=SqlAlchemyOutboxDispatchStore(SessionFactory),
        )
    finally:
        session.close()
