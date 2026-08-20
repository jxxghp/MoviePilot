"""
API 层的公共依赖。

包含两类：把请求级用例与其适配器装配起来的工厂，以及从安全服务取得的鉴权依赖。
端点统一经由本模块声明 Depends，不直接触达具体实现。
"""
from typing import Any

from fastapi import BackgroundTasks, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.schemas.token import TokenPayload as _SchemaTokenPayload
from app.application.subscription.delete import DeleteSubscribeCommand
from app.application.subscription.identity import (
    DeleteSubscriptionsByIdentityCommand,
)
from app.application.subscription.search import SearchSubscriptionsCommand
from app.application.subscription.query import SubscriptionQueryService
from app.application.subscription.mutation import SubscriptionMutationService
from app.application.site.mutation import SiteMutationCommand
from app.application.site.query import SiteQueryService
from app.application.workflow import (
    WorkflowDefinitionCommand,
    WorkflowMutationCommand,
    WorkflowQueryService,
)
from app.application.messaging.message import MessageQueryService
from app.application.messaging.chat import AgentChatService
from app.application.mediaserver import MediaServerQueryService
from app.application.servarr import ServarrSubscriptionService
from app.application.dashboard import DashboardQueryService
from app.application.history import (
    DownloadHistoryMutationCommand,
    HistoryQueryService,
    TransferHistoryLookupService,
    TransferHistoryMutationCommand,
    clear_transfer_failures,
)
from app.application.plugin.config import PluginConfigCommand
from app.application.commands import init_commands
from app.application.plugin.routes import register_plugin_api
from app.application.scheduling import update_plugin_job
from app.adapters.web.security.access import verify_token
from app.application.security.user import UserService
from app.application.security.auth import AuthService
from app.application.security.passkeys import PasskeyService
from app.application.security.identity import UserIdentityService
from app.adapters.external.server import MoviePilotServerHelper
from app.api.data import get_api_data_ports, get_async_db, get_db
from app.runtime.events import eventmanager
from app.application.plugin.runtime import get_plugin_manager as PluginManager
from app.runtime.log import logger
from app.schemas.event import PluginDataResetEventData
from app.schemas.types import ChainEventType, EventType
from app.application.scheduling import Scheduler
from app.application.site.sites import SitesHelper  # pylint: disable=no-name-in-module
from app.domain import site as site_rules
from app.foundation import url as url_tools
from app.runtime.config import global_vars
from app.workflow import WorkFlowManager
from app.application.orchestration.storage import StorageChain
from app.schemas.workflow import FileItem as _SchemaFileItem


def _repository(name: str, session: Any) -> Any:
    """构造绑定当前请求会话的数据仓储。"""
    return get_api_data_ports().repository(name, session)


def _standalone_repository(name: str) -> Any:
    """构造无需绑定请求会话的数据端口。"""
    return get_api_data_ports().standalone_repository(name)


def _transaction(name: str, session: Any) -> Any:
    """构造绑定当前请求会话的事务端口。"""
    return get_api_data_ports().transaction(name, session)


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
        repository=_repository("subscribe", db),
        unit_of_work=_transaction("async", db),
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
        repository=_repository("subscribe", db),
        unit_of_work=_transaction("async", db),
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
        repository=_repository("subscribe", db),
        schedule_search=schedule_search,
    )


def get_subscription_query_service(
        db: AsyncSession = Depends(get_async_db),
) -> SubscriptionQueryService:
    """组装订阅和订阅历史异步查询服务。"""
    return SubscriptionQueryService(
        repository=_repository("subscribe", db),
        async_repository=_repository("subscribe", db),
        history_repository=_repository("subscribe_history", db),
    )


def get_user_service(
        db: AsyncSession = Depends(get_async_db),
) -> UserService:
    """组装用户管理应用服务。"""
    return UserService(repository=_repository("user", db))


def get_auth_service() -> AuthService:
    """组装同步认证应用服务。"""
    return AuthService(
        users=_standalone_repository("user"),
        config=_standalone_repository("system_config"),
        passkeys=_standalone_repository("passkey"),
    )


def get_passkey_service() -> PasskeyService:
    """组装 PassKey 应用服务。"""
    return PasskeyService(repository=_standalone_repository("passkey"))


def get_user_identity_service() -> UserIdentityService:
    """组装第三方身份绑定应用服务。"""
    return UserIdentityService(repository=_standalone_repository("user_identity"))


def get_subscription_mutation_service(
        db: AsyncSession = Depends(get_async_db),
) -> SubscriptionMutationService:
    """组装异步订阅写服务。"""
    return SubscriptionMutationService(
        repository=_repository("subscribe", db),
        history_repository=_repository("subscribe_history", db),
    )


def get_subscription_sync_mutation_service(
        db: Session = Depends(get_db),
) -> SubscriptionMutationService:
    """组装同步订阅查询服务，供文件信息接口使用。"""
    return SubscriptionMutationService(repository=_repository("subscribe", db))


def get_servarr_subscription_service(
    async_db: AsyncSession = Depends(get_async_db),
    db: Session = Depends(get_db),
) -> ServarrSubscriptionService:
    """组装 Servarr 兼容路由的请求级订阅数据用例。"""
    return ServarrSubscriptionService(
        async_repository=_repository("subscribe", async_db),
        sync_repository=_repository("subscribe", db),
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
        repository=_repository("site", db),
        unit_of_work=_transaction("async", db),
        auth_level_provider=lambda: sites_helper.auth_level,
        indexer_loader=sites_helper.async_get_indexer,
        domain_extractor=site_rules.extract_domain,
        url_normalizer=normalize_url,
        publish_updated=_publish_site_updated,
        publish_deleted=_publish_site_deleted,
    )


def get_site_query_service(
        db: AsyncSession = Depends(get_async_db),
) -> SiteQueryService:
    """组装站点异步查询服务。"""
    return SiteQueryService(repository=_repository("site", db))


def get_site_sync_query_service(
        db: Session = Depends(get_db),
) -> SiteQueryService:
    """组装站点同步查询服务，用于同步 Chain 路由。"""
    return SiteQueryService(repository=_repository("site", db))


def get_workflow_mutation_command(
        db: Session = Depends(get_db),
) -> WorkflowMutationCommand:
    """组装请求级工作流写用例和提交后的调度副作用。"""
    scheduler = Scheduler()
    workflow_manager = WorkFlowManager()
    return WorkflowMutationCommand(
        repository=_repository("workflow", db),
        unit_of_work=_transaction("sync", db),
        add_timer=scheduler.update_workflow_job,
        remove_timer=scheduler.remove_workflow_job,
        load_event=workflow_manager.load_workflow_events,
        remove_event=workflow_manager.remove_workflow_event,
        refresh_event=workflow_manager.update_workflow_event,
        stop_running=global_vars.stop_workflow,
        delete_cache=lambda workflow_id: _standalone_repository("system_config").delete(
            f"WorkflowCache-{workflow_id}"
        ),
    )


def get_workflow_definition_command(
        db: AsyncSession = Depends(get_async_db),
) -> WorkflowDefinitionCommand:
    """组装工作流创建、复用和重置的异步写用例。"""
    return WorkflowDefinitionCommand(
        repository=_repository("workflow", db),
        unit_of_work=_transaction("async", db),
        stop_running=global_vars.stop_workflow,
        delete_cache=lambda workflow_id: _standalone_repository("system_config").delete(
            f"WorkflowCache-{workflow_id}"
        ),
        report_fork=MoviePilotServerHelper.async_workflow_fork_by_id,
    )


def get_workflow_query_service(
        db: AsyncSession = Depends(get_async_db),
) -> WorkflowQueryService:
    """组装工作流只读查询用例，避免端点直接持有数据库操作器。"""
    return WorkflowQueryService(repository=_repository("workflow", db))


def get_message_query_service(
        db: AsyncSession = Depends(get_async_db),
) -> MessageQueryService:
    """组装消息历史异步查询服务。"""
    return MessageQueryService(repository=_repository("message", db))


def get_agent_chat_service(
        db: AsyncSession = Depends(get_async_db),
) -> AgentChatService:
    """组装 Agent 会话历史查询和删除服务。"""
    return AgentChatService(repository=_repository("agent_chat", db))


def get_mediaserver_query_service(
        db: AsyncSession = Depends(get_async_db),
) -> MediaServerQueryService:
    """组装媒体服务器本地条目异步查询服务。"""
    return MediaServerQueryService(repository=_repository("media_server", db))


def get_dashboard_query_service(
        db: Session = Depends(get_db),
) -> DashboardQueryService:
    """组装 Dashboard 媒体与整理历史统计查询服务。"""
    from app.application.orchestration.dashboard import DashboardChain

    return DashboardQueryService(
        repository=_repository("transfer_history", db),
        media_statistics=DashboardChain().media_statistic,
    )


def get_download_history_mutation_command(
        db: Session = Depends(get_db),
) -> DownloadHistoryMutationCommand:
    """组装下载历史删除用例及其请求级事务。"""
    return DownloadHistoryMutationCommand(
        repository=_repository("download_history", db),
        unit_of_work=_transaction("sync", db),
    )


def get_history_query_service(
        db: AsyncSession = Depends(get_async_db),
) -> HistoryQueryService:
    """组装历史列表和详情异步查询服务。"""
    return HistoryQueryService(
        download_repository=_repository("download_history", db),
        transfer_repository=_repository("transfer_history", db),
    )


def get_transfer_history_lookup_service(
        db: Session = Depends(get_db),
) -> TransferHistoryLookupService:
    """组装手动整理使用的同步历史投影服务。"""
    return TransferHistoryLookupService(_repository("transfer_history", db))


def get_transfer_history_mutation_command(
        db: Session = Depends(get_db),
) -> TransferHistoryMutationCommand:
    """组装整理历史删除、文件处理和事件发布用例。"""
    storage_chain = StorageChain()
    return TransferHistoryMutationCommand(
        repository=_repository("transfer_history", db),
        download_repository=_repository("download_history", db),
        unit_of_work=_transaction("sync", db),
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
