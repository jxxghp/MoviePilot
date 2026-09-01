"""订阅领域的请求级 command/query 依赖。"""

from collections.abc import Mapping
from typing import cast

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.adapters.external.server import MoviePilotServerHelper
from app.api.context import (
    get_async_session,
    get_background_task_registry,
    get_host_runtime,
    get_subscription_history_repository,
    get_subscription_outbox,
    get_subscription_outbox_store,
    get_subscription_repository,
    get_subscription_transaction,
    get_sync_session,
    resolve_background_task_registry,
)
from app.application.outbox import AsyncOutboxDispatchStore, AsyncOutboxStager
from app.application.scheduling import start_scheduler_job
from app.application.servarr import ServarrSubscriptionService
from app.application.subscription.contract import (
    SessionSubscriptionPort,
    SubscriptionHistoryStagingPort,
    SubscriptionStagingPort,
)
from app.application.subscription.delete import (
    AsyncUnitOfWork as DeleteUnitOfWork,
)
from app.application.subscription.delete import (
    DeleteSubscribeCommand,
)
from app.application.subscription.execution import SubscriptionSearchRepository
from app.application.subscription.identity import (
    DeleteSubscriptionsByIdentityCommand,
)
from app.application.subscription.mutation import (
    SubscriptionMutationService,
)
from app.application.subscription.query import SubscriptionQueryService
from app.application.subscription.search import SearchSubscriptionsCommand
from app.application.subscription.status import SubscriptionExecutionStatusService
from app.application.subscription.write import (
    SubscriptionBatchWritePort,
)
from app.runtime.events import eventmanager
from app.runtime.log import logger
from app.runtime.tasks import TaskRegistry
from app.schemas.common import JsonData
from app.schemas.types import EventType
from app.startup.composition.context import HostRuntime


async def _publish_subscribe_deleted(
    payload: Mapping[str, JsonData],
) -> None:
    """通过宿主事件总线发布已提交的订阅删除事件。"""
    await eventmanager.async_send_event(EventType.SubscribeDeleted, payload)


async def _publish_subscribe_modified(payload: Mapping[str, JsonData]) -> None:
    """通过宿主事件总线发布已提交的订阅修改事件。"""
    await eventmanager.async_send_event(EventType.SubscribeModified, dict(payload))


async def _report_subscribe_deleted(payload: Mapping[str, JsonData]) -> bool:
    """把只读删除快照复制给远端上报边界。"""
    return bool(await MoviePilotServerHelper.async_sub_done_durable(dict(payload)))


def get_delete_subscribe_command(
    repository_port: SubscriptionStagingPort = Depends(get_subscription_repository),
    unit_of_work: object = Depends(get_subscription_transaction),
    outbox: AsyncOutboxStager = Depends(get_subscription_outbox),
    dispatch_store: AsyncOutboxDispatchStore = Depends(
        get_subscription_outbox_store
    ),
) -> DeleteSubscribeCommand:
    """组装请求级订阅删除用例及其具体适配器。"""
    return DeleteSubscribeCommand(
        repository=repository_port,
        unit_of_work=cast(DeleteUnitOfWork, unit_of_work),
        publish_deleted=_publish_subscribe_deleted,
        report_deleted=_report_subscribe_deleted,
        outbox=outbox,
        dispatch_store=dispatch_store,
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
    repository_port: SubscriptionStagingPort = Depends(get_subscription_repository),
    unit_of_work: object = Depends(get_subscription_transaction),
    outbox: AsyncOutboxStager = Depends(get_subscription_outbox),
    dispatch_store: AsyncOutboxDispatchStore = Depends(
        get_subscription_outbox_store
    ),
) -> DeleteSubscriptionsByIdentityCommand:
    """组装请求级按媒体身份删除订阅用例。"""
    return DeleteSubscriptionsByIdentityCommand(
        repository=repository_port,
        unit_of_work=cast(DeleteUnitOfWork, unit_of_work),
        publish_deleted=_publish_subscribe_deleted,
        handle_event_error=_log_subscribe_deleted_event_error,
        outbox=outbox,
        dispatch_store=dispatch_store,
    )


def _start_subscription_search_batch(
    subscribe_ids: tuple[int, ...] | None,
    state: str | None,
) -> None:
    """把一个请求的搜索目标作为同一调度任务提交。"""
    if subscribe_ids is None:
        start_scheduler_job(
            "subscribe_search",
            sid=None,
            state=state,
            manual=True,
        )
        return
    start_scheduler_job(
        "subscribe_search",
        sids=subscribe_ids,
        state=None,
        manual=True,
    )


def get_search_subscriptions_command(
    task_registry: TaskRegistry = Depends(get_background_task_registry),
    db: AsyncSession = Depends(get_async_session),
    runtime: HostRuntime = Depends(get_host_runtime),
) -> SearchSubscriptionsCommand:
    """组装手工订阅搜索用例，并把调度延迟到响应后的后台任务。"""
    def schedule_search(
        subscribe_ids: tuple[int, ...] | None,
        state: str | None,
    ) -> None:
        """把当前用户的搜索目标提交为一个顺序后台批次。"""
        resolve_background_task_registry(task_registry).create_sync(
            _start_subscription_search_batch,
            subscribe_ids,
            state,
            owner="api.subscribe.search",
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


def get_subscription_execution_status_service(
    db: AsyncSession = Depends(get_async_session),
    runtime: HostRuntime = Depends(get_host_runtime),
) -> SubscriptionExecutionStatusService:
    """组装请求级订阅执行状态投影服务。"""
    factory = runtime.subscription.execution_status_repository
    if factory is None:
        raise RuntimeError("订阅执行状态仓储未注册")
    repository = factory(db)
    return SubscriptionExecutionStatusService(repository=repository)  # type: ignore[arg-type]


def get_subscription_search_repository(
    runtime: HostRuntime = Depends(get_host_runtime),
) -> SubscriptionSearchRepository:
    """返回宿主组合根持有的订阅搜索队列端口。"""
    if runtime.subscription.search_repository is None:
        raise RuntimeError("订阅搜索队列未注册")
    return cast(SubscriptionSearchRepository, runtime.subscription.search_repository)


def get_subscription_mutation_service(
    repository_port: SessionSubscriptionPort = Depends(get_subscription_repository),
    history_repository: SubscriptionHistoryStagingPort = Depends(
        get_subscription_history_repository
    ),
    unit_of_work: object = Depends(get_subscription_transaction),
    outbox: AsyncOutboxStager = Depends(get_subscription_outbox),
    dispatch_store: AsyncOutboxDispatchStore = Depends(
        get_subscription_outbox_store
    ),
) -> SubscriptionMutationService:
    """组装异步订阅写服务。"""
    return SubscriptionMutationService(
        repository=repository_port,
        history_repository=history_repository,
        unit_of_work=cast(DeleteUnitOfWork, unit_of_work),
        outbox=outbox,
        dispatch_store=dispatch_store,
        publish_modified=_publish_subscribe_modified,
    )


def get_servarr_subscription_batch_writer(
    repository_port: SubscriptionStagingPort = Depends(get_subscription_repository),
    unit_of_work: object = Depends(get_subscription_transaction),
    outbox: AsyncOutboxStager = Depends(get_subscription_outbox),
    dispatch_store: AsyncOutboxDispatchStore = Depends(
        get_subscription_outbox_store
    ),
    runtime: HostRuntime = Depends(get_host_runtime),
) -> SubscriptionBatchWritePort:
    """组装 Servarr 多季订阅使用的请求级原子批量写端口。"""
    return runtime.subscription.batch_writer(
        repository=repository_port,
        unit_of_work=cast(DeleteUnitOfWork, unit_of_work),
        outbox=outbox,
        dispatch_store=dispatch_store,
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
