"""
API 层的公共依赖。

包含两类：把请求级用例与其适配器装配起来的工厂，以及从安全服务取得的鉴权依赖。
端点统一经由本模块声明 Depends，不直接触达具体实现。
"""
from fastapi import BackgroundTasks, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.application.subscription.delete import (
    DeleteSubscribeCommand,
    SubscribeDeletionCandidateRepository,
)
from app.application.subscription.identity import (
    DeleteSubscriptionsByIdentityCommand,
)
from app.application.subscription.search import SearchSubscriptionsCommand
from app.application.site.mutation import SiteMutationCommand
from app.application.workflow import (
    WorkflowDefinitionCommand,
    WorkflowMutationCommand,
)
from app.application.history import (
    DownloadHistoryMutationCommand,
    TransferHistoryMutationCommand,
    clear_transfer_failures,
)
from app.application.plugin.config import PluginConfigCommand
from app.application.commands import init_commands
from app.application.plugins import register_plugin_api
from app.application.scheduling import update_plugin_job
# 端点声明鉴权时统一从本模块取用的当前用户依赖族
from app.application.security.dependencies import (  # noqa: F401
    get_current_active_manage_user,
    get_current_active_manage_user_async,
    get_current_active_superuser,
    get_current_active_superuser_async,
    get_current_active_user,
    get_current_active_user_async,
    get_current_user,
    get_current_user_async,
)
from app.adapters.external.server import MoviePilotServerHelper
from app.db import get_async_db, get_db
from app.db.oper.subscribe import SubscribeOper
from app.db.oper.site import SiteOper
from app.db.uow import SqlAlchemyAsyncUnitOfWork, SqlAlchemyUnitOfWork
from app.runtime.events import eventmanager
from app.runtime.extensions.plugin_manager import PluginManager
from app.runtime.log import logger
from app.schemas.event import PluginDataResetEventData
from app.schemas.types import ChainEventType, EventType
from app.scheduler import Scheduler
from app.application.site.sites import SitesHelper  # pylint: disable=no-name-in-module
from app.domain import site as site_rules
from app.foundation import url as url_tools
from app.db.oper.systemconfig import SystemConfigOper
from app.db.oper.workflow import WorkflowOper
from app.db.oper.downloadhistory import DownloadHistoryOper
from app.db.oper.transferhistory import TransferHistoryOper
from app.runtime.config import global_vars
from app.workflow import WorkFlowManager
from app.application.orchestration.storage import StorageChain
from app.schemas.workflow import FileItem as _SchemaFileItem


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
        repository=SubscribeDeletionCandidateRepository(SubscribeOper(db)),
        unit_of_work=SqlAlchemyAsyncUnitOfWork(db),
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
        repository=SubscribeDeletionCandidateRepository(SubscribeOper(db)),
        unit_of_work=SqlAlchemyAsyncUnitOfWork(db),
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
        repository=SubscribeDeletionCandidateRepository(SubscribeOper(db)),
        schedule_search=schedule_search,
    )


async def _publish_site_updated(payload: dict) -> None:
    """发布已提交的站点更新事件。"""
    await eventmanager.async_send_event(EventType.SiteUpdated, payload)


async def _publish_site_deleted(payload: dict) -> None:
    """发布已提交的站点删除事件。"""
    await eventmanager.async_send_event(EventType.SiteDeleted, payload)


def get_site_mutation_command(
        db: AsyncSession = Depends(get_async_db),
) -> SiteMutationCommand:
    """组装请求级站点写用例及其事务和外部目录依赖。"""
    sites_helper = SitesHelper()

    def normalize_url(value: str) -> str:
        """沿用站点接口的 scheme/netloc 规范化格式。"""
        scheme, netloc = url_tools.split_netloc(value)
        return f"{scheme}://{netloc}/"

    return SiteMutationCommand(
        repository=SiteOper(db),
        unit_of_work=SqlAlchemyAsyncUnitOfWork(db),
        auth_level_provider=lambda: sites_helper.auth_level,
        indexer_loader=sites_helper.async_get_indexer,
        domain_extractor=site_rules.extract_domain,
        url_normalizer=normalize_url,
        publish_updated=_publish_site_updated,
        publish_deleted=_publish_site_deleted,
    )


def get_workflow_mutation_command(
        db: Session = Depends(get_db),
) -> WorkflowMutationCommand:
    """组装请求级工作流写用例和提交后的调度副作用。"""
    scheduler = Scheduler()
    workflow_manager = WorkFlowManager()
    return WorkflowMutationCommand(
        repository=WorkflowOper(db),
        unit_of_work=SqlAlchemyUnitOfWork(db),
        add_timer=scheduler.update_workflow_job,
        remove_timer=scheduler.remove_workflow_job,
        load_event=workflow_manager.load_workflow_events,
        remove_event=workflow_manager.remove_workflow_event,
        refresh_event=workflow_manager.update_workflow_event,
        stop_running=global_vars.stop_workflow,
        delete_cache=lambda workflow_id: SystemConfigOper().delete(
            f"WorkflowCache-{workflow_id}"
        ),
    )


def get_workflow_definition_command(
        db: AsyncSession = Depends(get_async_db),
) -> WorkflowDefinitionCommand:
    """组装工作流创建、复用和重置的异步写用例。"""
    return WorkflowDefinitionCommand(
        repository=WorkflowOper(db),
        unit_of_work=SqlAlchemyAsyncUnitOfWork(db),
        stop_running=global_vars.stop_workflow,
        delete_cache=lambda workflow_id: SystemConfigOper().delete(
            f"WorkflowCache-{workflow_id}"
        ),
        report_fork=MoviePilotServerHelper.async_workflow_fork_by_id,
    )


def get_download_history_mutation_command(
        db: Session = Depends(get_db),
) -> DownloadHistoryMutationCommand:
    """组装下载历史删除用例及其请求级事务。"""
    return DownloadHistoryMutationCommand(
        repository=DownloadHistoryOper(db),
        unit_of_work=SqlAlchemyUnitOfWork(db),
    )


def get_transfer_history_mutation_command(
        db: Session = Depends(get_db),
) -> TransferHistoryMutationCommand:
    """组装整理历史删除、文件处理和事件发布用例。"""
    storage_chain = StorageChain()
    return TransferHistoryMutationCommand(
        repository=TransferHistoryOper(db),
        download_repository=DownloadHistoryOper(db),
        unit_of_work=SqlAlchemyUnitOfWork(db),
        file_item_factory=lambda payload: _SchemaFileItem(**payload),
        delete_media_file=storage_chain.delete_media_file,
        publish_download_file_deleted=lambda payload: eventmanager.send_event(
            EventType.DownloadFileDeleted,
            payload,
        ),
        clear_failures=clear_transfer_failures,
    )


def get_plugin_config_command() -> PluginConfigCommand:
    """组装插件配置更新与重置用例，隔离 API 对运行时写操作的编排。"""
    manager = PluginManager()

    def publish_reset(plugin_id: str) -> None:
        """在清理持久化数据前通知目标插件执行补偿。"""
        eventmanager.send_event(
            ChainEventType.PluginDataReset,
            PluginDataResetEventData(
                plugin_id=plugin_id,
                reset_config=True,
                reset_data=True,
            ),
        )

    def refresh_registrations(plugin_id: str) -> None:
        """按服务、命令、动态路由顺序刷新插件宿主注册。"""
        update_plugin_job(plugin_id)
        init_commands(plugin_id)
        register_plugin_api(plugin_id)

    return PluginConfigCommand(
        save_config=manager.save_plugin_config,
        initialize=manager.init_plugin,
        stop=manager.stop,
        delete_config=manager.delete_plugin_config,
        delete_data=manager.delete_plugin_data,
        reload_runtime=manager.reload_plugin,
        publish_reset=publish_reset,
        refresh_registrations=refresh_registrations,
    )
