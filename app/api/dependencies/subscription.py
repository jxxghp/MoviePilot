"""订阅领域的请求级 command/query 依赖。"""

from typing import Any, cast

from fastapi import BackgroundTasks, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.adapters.external.server import MoviePilotServerHelper
from app.api.context import (
    get_async_session,
    get_host_runtime,
    get_subscription_history_repository,
    get_subscription_outbox,
    get_subscription_repository,
    get_subscription_transaction,
    get_sync_session,
)
from app.application.outbox import AsyncOutboxTransaction
from app.application.scheduling import start_scheduler_job
from app.application.servarr import ServarrSubscriptionService
from app.application.subscription.delete import (
    AsyncUnitOfWork as DeleteUnitOfWork,
    DeleteSubscribeCommand,
    SubscribeDeletionRepository,
)
from app.application.subscription.identity import DeleteSubscriptionsByIdentityCommand
from app.application.subscription.identity import SubscribeIdentityDeletionRepository
from app.application.subscription.mutation import (
    AsyncUnitOfWork as MutationUnitOfWork,
    SubscriptionHistoryMutationRepository,
    SubscriptionMutationRepository,
    SubscriptionMutationService,
)
from app.application.subscription.query import SubscriptionQueryService
from app.application.subscription.search import SearchSubscriptionsCommand
from app.runtime.events import eventmanager
from app.runtime.log import logger
from app.schemas.types import EventType
from app.startup.ports.context import HostRuntime


async def _publish_subscribe_deleted(
    payload: dict[str, Any],
) -> None:
    """通过宿主事件总线发布已提交的订阅删除事件。"""
    await eventmanager.async_send_event(EventType.SubscribeDeleted, payload)


async def _publish_subscribe_modified(payload: dict[str, Any]) -> None:
    """通过宿主事件总线发布已提交的订阅修改事件。"""
    await eventmanager.async_send_event(EventType.SubscribeModified, payload)


def get_delete_subscribe_command(
    repository_port: object = Depends(get_subscription_repository),
    unit_of_work: object = Depends(get_subscription_transaction),
    outbox: AsyncOutboxTransaction = Depends(get_subscription_outbox),
) -> DeleteSubscribeCommand:
    """组装请求级订阅删除用例及其具体适配器。"""
    return DeleteSubscribeCommand(
        repository=cast(SubscribeDeletionRepository, repository_port),
        unit_of_work=cast(DeleteUnitOfWork, unit_of_work),
        publish_deleted=_publish_subscribe_deleted,
        report_deleted=MoviePilotServerHelper.sub_done_async,
        outbox=outbox,
    )


def _log_subscribe_deleted_event_error(
    subscribe_id: int,
    error: Exception,
) -> None:
    """记录按媒体身份删除时的单条事件失败并允许后续事件继续。"""
    logger.error(
        f"发送订阅删除事件失败：{subscribe_id} - {error}",
        exc_info=True,
    )


def get_delete_subscriptions_by_identity_command(
    repository_port: object = Depends(get_subscription_repository),
    unit_of_work: object = Depends(get_subscription_transaction),
    outbox: AsyncOutboxTransaction = Depends(get_subscription_outbox),
) -> DeleteSubscriptionsByIdentityCommand:
    """组装请求级按媒体身份删除订阅用例。"""
    return DeleteSubscriptionsByIdentityCommand(
        repository=cast(SubscribeIdentityDeletionRepository, repository_port),
        unit_of_work=cast(DeleteUnitOfWork, unit_of_work),
        publish_deleted=_publish_subscribe_deleted,
        handle_event_error=_log_subscribe_deleted_event_error,
        outbox=outbox,
    )


def get_search_subscriptions_command(
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_async_session),
    runtime: HostRuntime = Depends(get_host_runtime),
) -> SearchSubscriptionsCommand:
    """组装手工订阅搜索用例，并把调度延迟到响应后的后台任务。"""
    def schedule_search(subscribe_id: int | None, state: str | None) -> None:
        """按历史参数提交订阅搜索调度任务。"""
        background_tasks.add_task(
            start_scheduler_job,
            job_id="subscribe_search",
            sid=subscribe_id,
            state=state,
            manual=True,
        )

    return SearchSubscriptionsCommand(
        repository=runtime.subscription.repository(db),
        schedule_search=schedule_search,
    )


def get_subscription_query_service(
    db: AsyncSession = Depends(get_async_session),
    runtime: HostRuntime = Depends(get_host_runtime),
) -> SubscriptionQueryService:
    """组装订阅和订阅历史异步查询服务。"""
    return SubscriptionQueryService(
        repository=runtime.subscription.repository(db),
        async_repository=runtime.subscription.repository(db),
        history_repository=runtime.subscription.history_repository(db),
    )


def get_subscription_mutation_service(
    repository_port: object = Depends(get_subscription_repository),
    history_repository: SubscriptionHistoryMutationRepository = Depends(
        get_subscription_history_repository
    ),
    unit_of_work: object = Depends(get_subscription_transaction),
    outbox: AsyncOutboxTransaction = Depends(get_subscription_outbox),
) -> SubscriptionMutationService:
    """组装异步订阅写服务。"""
    return SubscriptionMutationService(
        repository=cast(SubscriptionMutationRepository, repository_port),
        history_repository=history_repository,
        unit_of_work=cast(MutationUnitOfWork, unit_of_work),
        outbox=outbox,
        publish_modified=_publish_subscribe_modified,
    )


def get_subscription_sync_mutation_service(
    db: Session = Depends(get_sync_session),
    runtime: HostRuntime = Depends(get_host_runtime),
) -> SubscriptionMutationService:
    """组装同步订阅查询服务，供文件信息接口使用。"""
    return SubscriptionMutationService(
        repository=cast(
            SubscriptionMutationRepository,
            runtime.subscription.repository(db),
        )
    )


def get_servarr_subscription_service(
    async_db: AsyncSession = Depends(get_async_session),
    db: Session = Depends(get_sync_session),
    runtime: HostRuntime = Depends(get_host_runtime),
) -> ServarrSubscriptionService:
    """组装 Servarr 兼容路由的请求级订阅数据用例。"""
    return ServarrSubscriptionService(
        async_repository=runtime.subscription.repository(async_db),
        sync_repository=runtime.subscription.repository(db),
    )
