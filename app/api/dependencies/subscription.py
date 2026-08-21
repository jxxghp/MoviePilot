"""订阅领域的请求级 command/query 依赖。"""

from fastapi import BackgroundTasks, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.adapters.external.server import MoviePilotServerHelper
from app.api.data import get_async_db, get_db
from app.api.dependencies.data import repository, transaction
from app.application.scheduling import Scheduler
from app.application.servarr import ServarrSubscriptionService
from app.application.subscription.delete import DeleteSubscribeCommand
from app.application.subscription.identity import DeleteSubscriptionsByIdentityCommand
from app.application.subscription.mutation import SubscriptionMutationService
from app.application.subscription.query import SubscriptionQueryService
from app.application.subscription.search import SearchSubscriptionsCommand
from app.runtime.events import eventmanager
from app.runtime.log import logger
from app.schemas.types import EventType


async def _publish_subscribe_deleted(
    subscribe_id: int,
    subscribe_info: dict,
) -> None:
    """通过宿主事件总线发布已提交的订阅删除事件。"""
    await eventmanager.async_send_event(
        EventType.SubscribeDeleted,
        {"subscribe_id": subscribe_id, "subscribe_info": subscribe_info},
    )


def get_delete_subscribe_command(
    db: AsyncSession = Depends(get_async_db),
) -> DeleteSubscribeCommand:
    """组装请求级订阅删除用例及其具体适配器。"""
    return DeleteSubscribeCommand(
        repository=repository("subscribe", db),
        unit_of_work=transaction("async", db),
        publish_deleted=_publish_subscribe_deleted,
        report_deleted=MoviePilotServerHelper.sub_done_async,
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
    db: AsyncSession = Depends(get_async_db),
) -> DeleteSubscriptionsByIdentityCommand:
    """组装请求级按媒体身份删除订阅用例。"""
    return DeleteSubscriptionsByIdentityCommand(
        repository=repository("subscribe", db),
        unit_of_work=transaction("async", db),
        publish_deleted=_publish_subscribe_deleted,
        handle_event_error=_log_subscribe_deleted_event_error,
    )


def get_search_subscriptions_command(
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_async_db),
) -> SearchSubscriptionsCommand:
    """组装手工订阅搜索用例，并把调度延迟到响应后的后台任务。"""
    def schedule_search(subscribe_id: int | None, state: str | None) -> None:
        """按历史参数提交订阅搜索调度任务。"""
        background_tasks.add_task(
            Scheduler().start,
            job_id="subscribe_search",
            sid=subscribe_id,
            state=state,
            manual=True,
        )

    return SearchSubscriptionsCommand(
        repository=repository("subscribe", db),
        schedule_search=schedule_search,
    )


def get_subscription_query_service(
    db: AsyncSession = Depends(get_async_db),
) -> SubscriptionQueryService:
    """组装订阅和订阅历史异步查询服务。"""
    return SubscriptionQueryService(
        repository=repository("subscribe", db),
        async_repository=repository("subscribe", db),
        history_repository=repository("subscribe_history", db),
    )


def get_subscription_mutation_service(
    db: AsyncSession = Depends(get_async_db),
) -> SubscriptionMutationService:
    """组装异步订阅写服务。"""
    return SubscriptionMutationService(
        repository=repository("subscribe", db),
        history_repository=repository("subscribe_history", db),
    )


def get_subscription_sync_mutation_service(
    db: Session = Depends(get_db),
) -> SubscriptionMutationService:
    """组装同步订阅查询服务，供文件信息接口使用。"""
    return SubscriptionMutationService(repository=repository("subscribe", db))


def get_servarr_subscription_service(
    async_db: AsyncSession = Depends(get_async_db),
    db: Session = Depends(get_db),
) -> ServarrSubscriptionService:
    """组装 Servarr 兼容路由的请求级订阅数据用例。"""
    return ServarrSubscriptionService(
        async_repository=repository("subscribe", async_db),
        sync_repository=repository("subscribe", db),
    )
