import asyncio
import inspect
import sys
from collections.abc import Mapping
from functools import partial
from typing import Any, Callable, Optional, cast

from app.adapters.cache.redis import AsyncRedisHelper, RedisHelper
from app.adapters.network.http import AsyncRequestUtils, RequestUtils
from app.adapters.network.ip import IpUtils
from app.application.plugin.transaction import (
    PluginPersistenceService,
    configure_plugin_persistence,
)
from app.chain.mediaserver import MediaServerChain
from app.chain.tmdb import TmdbChain
from app.db.adapters.pluginidentity import TransactionalPluginIdentityStore
from app.db.adapters.plugininstallation import TransactionalPluginInstallationStore

# SitesHelper涉及资源包拉取，提前引入并容错提示
try:
    from app.application.site.sites import SitesHelper  # noqa  # pylint: disable=import-error,no-name-in-module
except ImportError as e:
    SitesHelper = None
    error_message = f"错误: {str(e)}\n站点认证及索引相关资源导入失败，请尝试重建容器或手动拉取资源"
    print(error_message, file=sys.stderr)
    sys.exit(1)

from app.adapters.external.server import (
    MoviePilotServerHelper,
    configure_server_application_services,
)
from app.adapters.network.doh import DohHelper
from app.adapters.system.host import SystemUtils
from app.adapters.system.resource import (
    ResourceHelper,
    configure_resource_version_provider,
)
from app.adapters.web.security.access import set_superuser_token_payload_provider
from app.api.data import ApiDataPorts, configure_api_data_runtime
from app.application.agent import AgentDataContext
from app.application.agenttask import (
    AgentTaskExecutionService,
    configure_agent_task_execution,
)
from app.application.chain.context import (
    ChainRuntimeContext,
    configure_chain_runtime_context_provider,
)
from app.application.chain.events import (
    restore_download_added,
    restore_transfer_result,
)
from app.application.configuration import (
    RuntimeConfiguration,
    RuntimeSettingsService,
    SystemConfigService,
    TransferRetryConfig,
    configure_runtime_configuration,
    configure_runtime_settings,
    configure_system_config,
    configure_token_runtime_config,
    configure_transfer_retry_config,
    get_configured_system_config,
)
from app.application.database import configure_database_governance
from app.application.history import configure_transfer_history_repository
from app.application.image import (
    ImageResponsePort,
    ImageTransport,
    InternalAddressPort,
    configure_image_ports,
    configure_wallpaper_providers,
)
from app.application.messaging.agent import (
    dispatch_web_agent_message_event,
    shutdown_web_agent_background_tasks,
    wait_web_agent_background_tasks,
)
from app.application.messaging.chat import (
    AgentChatPersistenceService,
    AgentChatService,
    configure_agent_chat_persistence,
    configure_agent_chat_service,
    get_configured_agent_chat_persistence,
)
from app.application.messaging.ingress import (
    MessageIngressPort,
    configure_message_ingress_port,
)
from app.application.messaging.message import (
    MessageHelper,
    MessageQueueManager,
    stop_message,
)
from app.application.module import configure_module_runtime
from app.application.outbox import (
    ClaimedOutboxMessage,
    OutboxDispatcher,
    configure_outbox_dispatcher,
    durable_event_topic,
    validate_durable_event_handlers,
)
from app.application.plugin.runtime import configure_plugin_runtime
from app.application.query import (
    DataQueryService,
    configure_data_query_service,
)
from app.application.security.auth import AuthService, build_superuser_token_payload, configure_auth_service
from app.application.security.passkey import (
    PASSKEY_CHALLENGE_TTL_SECONDS,
    PasskeyService,
    configure_passkey_challenge_cache,
    configure_passkey_service,
)
from app.application.security.url import close_image_proxy_block_log_coalescer
from app.application.security.user import configure_user_lookups
from app.application.security.userconfig import (
    UserConfigurationService,
    configure_user_configuration,
)
from app.application.server.report import ServerReportService
from app.application.server.share import ServerSharingService
from app.application.service import configure_service_directory
from app.application.site.health import SiteHealthService, configure_site_health_service
from app.application.site.query import SiteQueryService, configure_site_query_service
from app.application.subscription.contract import SubscriptionRepository
from app.application.workflow import (
    WorkflowQueryService,
    configure_workflow_execution,
    configure_workflow_query,
)
from app.command import CommandChain
from app.db.adapters.agent import (
    SessionAgentTaskRepository,
    TransactionalAgentTaskRepository,
    TransactionalPluginDataRepository,
)
from app.db.adapters.chain import TransactionalChainDurableEventWriter
from app.db.adapters.configuration import TransactionalUserConfigurationRepository
from app.db.adapters.download import TransactionalDownloadFailureRepository
from app.db.adapters.history.download import (
    SessionDownloadHistoryRepository,
    TransactionalDownloadHistoryRepository,
)
from app.db.adapters.history.transfer import (
    SessionTransferHistoryRepository,
    TransactionalTransferHistoryRepository,
)
from app.db.adapters.mediaserver import TransactionalMediaServerRepository
from app.db.adapters.outbox import (
    SqlAlchemyAsyncOutboxDispatchStore,
    SqlAlchemyAsyncOutboxStager,
    SqlAlchemyOutboxDispatchStore,
)
from app.db.adapters.query import SqlAlchemyDataQueryAdapter
from app.db.adapters.site import SessionSiteRepository, TransactionalSiteRepository
from app.db.adapters.subscription import (
    SessionSubscriptionHistoryRepository,
    SessionSubscriptionRepository,
    TransactionalSubscriptionHistoryRepository,
    TransactionalSubscriptionRepository,
)
from app.db.adapters.transaction import TransactionalWriteRunner
from app.db.adapters.transfer.admission import TransactionalTransferAdmissionRepository
from app.db.adapters.transfer.execution import (
    TransactionalTransferExecutionRepository,
)
from app.db.adapters.user import (
    SqlAlchemyUserRepository,
    TransactionalUserRepository,
)
from app.db.adapters.workflow import (
    TransactionalWorkflowExecutionService,
    TransactionalWorkflowQueryRepository,
)
from app.db.oper.agentchat import AgentChatOper
from app.db.oper.mediaserver import MediaServerOper
from app.db.oper.message import MessageOper
from app.db.oper.passkey import PassKeyOper
from app.db.oper.systemconfig import SystemConfigOper
from app.db.oper.workflow import WorkflowOper
from app.db.session import (
    SessionFactory,
    async_session_scope,
    close_database,
    get_async_db,
    get_db,
)
from app.db.uow import (
    SqlAlchemyAsyncUnitOfWork,
    SqlAlchemyUnitOfWork,
    configure_transaction_runners,
)
from app.db.worker import DatabaseWorker
from app.runtime.cache import AsyncFileCache, FileCache, TTLCache
from app.runtime.config import settings as legacy_settings
from app.runtime.events import EventHandlerBinding, EventManager
from app.runtime.execution import run_in_threadpool_to_completion
from app.runtime.extensions.module.dispatcher import ModuleInvocationDispatcher
from app.runtime.extensions.module_manager import ModuleManager
from app.runtime.extensions.plugin_manager import PluginManager
from app.runtime.extensions.service_config import (
    ServiceConfigHelper,
    configure_service_config_reader,
)
from app.runtime.log import logger
from app.runtime.observability import record_metric
from app.runtime.settings import (
    configure_runtime_setting_provider,
    configure_runtime_setting_updater,
    get_runtime_setting,
)
from app.runtime.state import SystemHelper
from app.runtime.stop import runtime_stop_state
from app.runtime.tasks import get_task_registry
from app.runtime.thread import ThreadHelper
from app.schemas.message import Message, MessageType
from app.schemas.types import EventType, SystemConfigKey
from app.startup.composition.configuration import (
    build_api_runtime_config,
    build_chain_runtime_config,
    build_scheduler_runtime_config,
    build_token_runtime_config,
)
from app.startup.composition.context import (
    AgentChatRuntime,
    AuthenticationRuntime,
    HistoryRuntime,
    HostRuntime,
    MessagingRuntime,
    PersistenceRuntime,
    SiteRuntime,
    SubscriptionRuntime,
    WorkflowRuntime,
)
from app.startup.composition.database import build_database_governance
from app.startup.composition.subscription import (
    async_rule_group_mutation_scope,
    build_subscription_batch_writer,
    delete_subscribe_scope,
    rule_group_mutation_scope,
    site_reference_mutation_scope,
    subscription_completion_scope,
    subscription_mutation_scope,
    sync_delete_subscribe_scope,
    sync_subscription_mutation_scope,
)
from app.startup.initializers.agent import configure_agent_data_context, init_agent
from app.startup.initializers.resources import (
    init_managed_resources,
    stop_managed_resources,
)
from app.startup.initializers.scheduler import configure_scheduler_agent_tasks

_database_worker: DatabaseWorker | None = None


async def stop_database_worker() -> None:
    """停止当前进程的数据库短事务 worker。"""
    global _database_worker
    worker = _database_worker
    if worker is not None:
        await worker.shutdown()
        _database_worker = None


async def _initialize_configuration_services(
    database_worker: DatabaseWorker,
) -> SystemConfigOper:
    """加载完整配置快照后发布系统与用户配置服务。"""
    system_config = SystemConfigOper()
    user_config = TransactionalUserConfigurationRepository(SessionFactory)
    await database_worker.run(system_config.load_snapshot)
    await database_worker.run(user_config.load_snapshot)
    configure_system_config(
        SystemConfigService(
            repository=system_config,
            async_executor=database_worker,
        )
    )
    configure_user_configuration(
        UserConfigurationService(
            repository=user_config,
            async_executor=database_worker,
        )
    )
    return system_config


def _build_runtime_settings_service() -> RuntimeSettingsService:
    """将唯一可变部署配置实现注入管理服务。"""
    return RuntimeSettingsService(legacy_settings)


def _build_transactional_user_repository() -> TransactionalUserRepository:
    """构造供 Chain、Agent 与进程级认证共享的短会话用户仓储。"""
    return TransactionalUserRepository(
        sync_session=SessionFactory,
        async_session=async_session_scope,
    )


def _execute_legacy_transfer_command(**kwargs: Any) -> Any:
    """把旧 Chain ABI 延迟转入唯一 TransferChain durable command。"""
    from app.chain.transfer.facade import TransferChain

    return TransferChain().execute_legacy_transfer_command(**kwargs)


def _build_chain_runtime_context(
    *,
    message_helper: MessageHelper,
    message_queue: MessageQueueManager,
    system_config: SystemConfigOper,
    site: TransactionalSiteRepository,
    subscription: TransactionalSubscriptionRepository,
    download_history: TransactionalDownloadHistoryRepository,
    transfer_history: TransactionalTransferHistoryRepository,
) -> ChainRuntimeContext:
    """在启动组合根创建 Chain 所需的运行时对象和数据端口。"""
    return ChainRuntimeContext(
        module_manager=ModuleManager(),
        plugin_manager=PluginManager(),
        event_manager=EventManager(),
        message_oper=MessageOper(),
        message_helper=message_helper,
        file_cache=FileCache(),
        async_file_cache=AsyncFileCache(),
        message_queue=message_queue,
        module_dispatcher_factory=ModuleInvocationDispatcher,
        site_repository=site,
        subscription_repository=subscription,
        subscription_mutation_scope=subscription_mutation_scope,
        sync_subscription_mutation_scope=sync_subscription_mutation_scope,
        subscription_delete_scope=delete_subscribe_scope,
        sync_subscription_delete_scope=sync_delete_subscribe_scope,
        subscription_completion_scope=subscription_completion_scope,
        rule_group_mutation_scope=partial(
            rule_group_mutation_scope,
            system_config.publish_many,
        ),
        site_reference_mutation_scope=partial(
            site_reference_mutation_scope,
            system_config.publish_many,
        ),
        download_history_repository=download_history,
        transfer_history_repository=transfer_history,
        transfer_admission_repository=TransactionalTransferAdmissionRepository(SessionFactory),
        transfer_execution_repository=TransactionalTransferExecutionRepository(SessionFactory),
        media_server_repository=TransactionalMediaServerRepository(SessionFactory),
        download_failure_repository=TransactionalDownloadFailureRepository(SessionFactory),
        user_repository=_build_transactional_user_repository(),
        legacy_transfer_command=_execute_legacy_transfer_command,
        configuration=build_chain_runtime_config(legacy_settings),
        durable_event_writer=TransactionalChainDurableEventWriter(SessionFactory),
        stop_state=runtime_stop_state,
    )


def configure_runtime_data_providers(
    workflow_query: WorkflowQueryService,
    subscription_repository: SubscriptionRepository,
) -> None:
    """在启动组合层装配运行时和外部服务所需的数据库读取能力。"""
    configure_service_config_reader(lambda key: get_configured_system_config().get(key))
    configure_module_runtime(lambda: ModuleManager())
    configure_plugin_runtime(lambda: PluginManager())
    configure_service_directory(
        configs=ServiceConfigHelper.get_configs,
        modules=lambda module_type: ModuleManager().get_running_type_modules(module_type),
    )
    configure_server_application_services(
        report_service=ServerReportService(
            config_reader=lambda key: get_configured_system_config().get(key),
            config_writer=lambda key, value: get_configured_system_config().set(key, value),
            async_config_writer=lambda key, value: get_configured_system_config().async_set(key, value),
            installed_plugins_provider=lambda: (
                get_configured_system_config().get(SystemConfigKey.UserInstalledPlugins) or []
            ),
            subscribes_provider=subscription_repository.list,
            async_subscribes_provider=subscription_repository.async_list,
            plugin_report_sender=MoviePilotServerHelper.plugin_install_report,
            async_plugin_report_sender=(MoviePilotServerHelper.async_plugin_install_report),
            subscribe_report_sender=MoviePilotServerHelper.subscribe_report,
            async_subscribe_report_sender=MoviePilotServerHelper.async_subscribe_report,
            repo_url_sanitizer=MoviePilotServerHelper.sanitize_plugin_repo_url,
        ),
        sharing_service=ServerSharingService(
            subscribe_provider=subscription_repository.get,
            async_subscribe_provider=subscription_repository.async_get,
            workflow_provider=workflow_query.get_sync,
            async_workflow_provider=workflow_query.get,
            user_uuid_provider=MoviePilotServerHelper.get_user_uuid,
            subscribe_sender=MoviePilotServerHelper.subscribe_share,
            async_subscribe_sender=MoviePilotServerHelper.async_subscribe_share,
            workflow_sender=MoviePilotServerHelper.workflow_share,
            async_workflow_sender=MoviePilotServerHelper.async_workflow_share,
            response_handler=MoviePilotServerHelper._handle_response,
            subscribe_cache_clearer=(MoviePilotServerHelper._clear_subscribe_share_cache),
            workflow_cache_clearer=(MoviePilotServerHelper._clear_workflow_share_cache),
        ),
    )


def _build_outbox_handlers() -> dict[
    str,
    Callable[[ClaimedOutboxMessage], None],
]:
    """构造等待真实执行边界的 at-least-once 通知、事件和统计 handler。"""

    def discard_event_receipt(_event: object) -> None:
        """丢弃普通事件 API 的回执，使 outbox handler 仅表达结算成功。"""

    def dispatch_subscribe_deleted_report(message: ClaimedOutboxMessage) -> None:
        """重放订阅删除统计；未确认时抛错以进入有限重试。"""
        if not MoviePilotServerHelper.sub_done_durable(message.payload.get("subscribe_info") or {}):
            raise RuntimeError("订阅删除统计上报未确认")

    def dispatch_subscribe_added_report(message: ClaimedOutboxMessage) -> None:
        """重放订阅新增统计；未确认时抛错以进入有限重试。"""
        if not MoviePilotServerHelper.sub_reg_durable(message.payload.get("subscribe_info") or {}):
            raise RuntimeError("订阅新增统计上报未确认")

    def dispatch_subscribe_complete_report(message: ClaimedOutboxMessage) -> None:
        """重放订阅完成统计；未确认时抛错以进入有限重试。"""
        if not MoviePilotServerHelper.sub_done_durable(message.payload.get("subscribe_info") or {}):
            raise RuntimeError("订阅完成统计上报未确认")

    def dispatch_subscribe_notification(message: ClaimedOutboxMessage) -> None:
        """恢复订阅完成通知；消息快照无需重建领域对象。"""
        snapshot = message.payload.get("message") or {}
        if not isinstance(snapshot, dict):
            raise RuntimeError("订阅完成通知快照格式无效")
        CommandChain().post_message_strict(
            Message.model_validate(snapshot),
            event_key=message.event_key,
        )

    def dispatch_subscribe_added_notification(message: ClaimedOutboxMessage) -> None:
        """恢复订阅新增通知；恢复使用提交前冻结的渲染消息快照。"""
        snapshot = message.payload.get("message") or {}
        if not isinstance(snapshot, dict):
            raise RuntimeError("订阅新增通知快照格式无效")
        CommandChain().post_message_strict(
            Message.model_validate(snapshot),
            event_key=message.event_key,
        )

    handlers: dict[str, Callable[[ClaimedOutboxMessage], None]] = {
        durable_event_topic(EventType.SubscribeAdded): lambda message: discard_event_receipt(
            EventManager().send_event_strict(
                EventType.SubscribeAdded,
                message.payload,
            )
        ),
        "subscribe.added.report": dispatch_subscribe_added_report,
        "subscribe.added.notification": dispatch_subscribe_added_notification,
        durable_event_topic(EventType.SubscribeModified): lambda message: discard_event_receipt(
            EventManager().send_event_strict(
                EventType.SubscribeModified,
                message.payload,
            )
        ),
        durable_event_topic(EventType.SubscribeDeleted): lambda message: discard_event_receipt(
            EventManager().send_event_strict(
                EventType.SubscribeDeleted,
                message.payload,
            )
        ),
        "subscribe.deleted.report": dispatch_subscribe_deleted_report,
        durable_event_topic(EventType.SubscribeComplete): lambda message: discard_event_receipt(
            EventManager().send_event_strict(
                EventType.SubscribeComplete,
                message.payload,
            )
        ),
        "subscribe.complete.report": dispatch_subscribe_complete_report,
        "subscribe.complete.notification": dispatch_subscribe_notification,
        durable_event_topic(EventType.DownloadAdded): lambda message: discard_event_receipt(
            EventManager().send_event_strict(
                EventType.DownloadAdded,
                restore_download_added(message.payload),
            )
        ),
        durable_event_topic(EventType.TransferComplete): lambda message: discard_event_receipt(
            EventManager().send_event_strict(
                EventType.TransferComplete,
                restore_transfer_result(message.payload),
            )
        ),
        durable_event_topic(EventType.TransferFailed): lambda message: discard_event_receipt(
            EventManager().send_event_strict(
                EventType.TransferFailed,
                restore_transfer_result(message.payload),
            )
        ),
        durable_event_topic(EventType.SubtitleTransferComplete): lambda message: discard_event_receipt(
            EventManager().send_event_strict(
                EventType.SubtitleTransferComplete,
                restore_transfer_result(message.payload),
            )
        ),
        durable_event_topic(EventType.SubtitleTransferFailed): lambda message: discard_event_receipt(
            EventManager().send_event_strict(
                EventType.SubtitleTransferFailed,
                restore_transfer_result(message.payload),
            )
        ),
        durable_event_topic(EventType.AudioTransferComplete): lambda message: discard_event_receipt(
            EventManager().send_event_strict(
                EventType.AudioTransferComplete,
                restore_transfer_result(message.payload),
            )
        ),
        durable_event_topic(EventType.AudioTransferFailed): lambda message: discard_event_receipt(
            EventManager().send_event_strict(
                EventType.AudioTransferFailed,
                restore_transfer_result(message.payload),
            )
        ),
    }
    validate_durable_event_handlers(handlers)
    return handlers


def _build_outbox_dispatcher() -> OutboxDispatcher:
    """创建使用独立短事务和 attempt fencing 的恢复 dispatcher。"""
    return OutboxDispatcher(
        repository=SqlAlchemyOutboxDispatchStore(SessionFactory),
        handlers=_build_outbox_handlers(),
        failure_observer=lambda dead: record_metric(
            "scheduler.job.dead_letter" if dead else "scheduler.job.retry",
            owner="outbox",
        ),
    )


def configure_wallpaper_services() -> None:
    """把需要 Chain 编排的壁纸来源注入图片服务。"""
    configure_wallpaper_providers(
        tmdb_wallpaper=lambda: TmdbChain().get_random_wallpager(),
        tmdb_wallpapers=lambda count: TmdbChain().get_trending_wallpapers(count),
        mediaserver_wallpaper=lambda: MediaServerChain().get_latest_wallpaper(),
        mediaserver_wallpapers=lambda count: MediaServerChain().get_latest_wallpapers(count=count),
    )


class _ImageTransportAdapter:
    """把通用 HTTP Adapter 收窄为图片应用服务的 GET 端口。"""

    def get(
        self,
        url: str,
        *,
        options: Mapping[str, Any],
    ) -> Optional[ImageResponsePort]:
        """同步创建短生命周期请求对象并返回响应。"""
        response = RequestUtils(**dict(options)).get_res(url=url)
        return cast(Optional[ImageResponsePort], response)

    async def async_get(
        self,
        url: str,
        *,
        options: Mapping[str, Any],
    ) -> Optional[ImageResponsePort]:
        """异步创建短生命周期请求对象并返回响应。"""
        response = await AsyncRequestUtils(**dict(options)).get_res(url=url)
        return cast(Optional[ImageResponsePort], response)


class _InternalAddressAdapter:
    """把通用地址判断收窄为图片代理决策端口。"""

    @staticmethod
    def is_internal(url: str) -> bool:
        """返回 URL 是否指向内部地址。"""
        probe = cast(Callable[[str], bool], IpUtils.is_internal)
        return bool(probe(url))


class _MessageIngressAdapter:
    """通过通用 HTTP Adapter 投递本地消息并负责释放响应。"""

    def post(
        self,
        url: str,
        payload: Mapping[str, Any],
        *,
        timeout: float,
    ) -> Optional[int]:
        """同步投递消息，关闭响应后返回状态码。"""
        response = RequestUtils(timeout=timeout).post_res(  # type: ignore[arg-type]
            url,
            json=dict(payload),
        )
        if response is None:
            return None
        try:
            return int(response.status_code)
        finally:
            try:
                response.close()
            except Exception as error:  # noqa: BLE001 - 释放失败不改变投递结果
                logger.debug(f"释放本地消息入口响应失败：{error}")

    async def async_post(
        self,
        url: str,
        payload: Mapping[str, Any],
        *,
        timeout: float,
    ) -> Optional[int]:
        """异步投递消息，关闭响应后返回状态码。"""
        response = await AsyncRequestUtils(
            timeout=timeout  # type: ignore[arg-type]
        ).post_res(
            url,
            json=dict(payload),
        )
        if response is None:
            return None
        try:
            return int(response.status_code)
        finally:
            try:
                await response.aclose()
            except Exception as error:  # noqa: BLE001 - 释放失败不改变投递结果
                logger.debug(f"释放本地消息入口响应失败：{error}")


def configure_application_network_ports() -> None:
    """装配图片读取、内部地址判断和消息回环传输端口。"""
    image_transport: ImageTransport = _ImageTransportAdapter()
    internal_address: InternalAddressPort = _InternalAddressAdapter()
    message_ingress: MessageIngressPort = _MessageIngressAdapter()
    configure_image_ports(
        transport=image_transport,
        internal_address=internal_address,
    )
    configure_message_ingress_port(message_ingress)


def notify_event_error(title: str, message: str) -> None:
    """将事件总线错误转发到系统消息通道。"""
    MessageHelper().put(
        title=title,
        message=message,
        role="system",
    )


def get_host_event_handler_factories() -> dict[type, Callable[[], object]]:
    """返回所有使用事件装饰器的宿主类及其明确实例工厂。"""
    from app.chain.download import DownloadChain
    from app.chain.scraping import ScrapingChain
    from app.chain.search import SearchChain
    from app.chain.site import SiteChain
    from app.chain.subscribe.facade import SubscribeChain
    from app.chain.workflow import WorkflowChain
    from app.command import Command
    from app.scheduler.facade import Scheduler

    return {
        Command: Command,
        DownloadChain: DownloadChain,
        Scheduler: Scheduler,
        ScrapingChain: ScrapingChain,
        SearchChain: SearchChain,
        SiteChain: SiteChain,
        SubscribeChain: SubscribeChain,
        WorkflowChain: WorkflowChain,
    }


def configure_host_event_handler_resolver() -> None:
    """显式登记宿主内置类处理器，禁止事件总线按类名临时构造未知对象。"""
    factories = get_host_event_handler_factories()

    def resolve(owner_class: type) -> EventHandlerBinding | None:
        """按明确白名单复用单例或构造与旧路径等价的 Chain 实例。"""
        factory = factories.get(owner_class)
        if factory is None:
            return None
        get_existing = getattr(owner_class, "get_existing_instance", None)
        instance = get_existing() if callable(get_existing) else None
        if instance is None:
            instance = factory()
        return EventHandlerBinding(
            instance=instance,
            owner_name=owner_class.__name__,
        )

    EventManager().register_handler_instance_resolver("host", resolve)


def start_frontend():
    """
    启动前端服务
    """
    # 仅Windows可执行文件支持内嵌nginx
    if not SystemUtils.is_frozen() or not SystemUtils.is_windows():
        return
    # 临时Nginx目录
    nginx_path = get_runtime_setting("ROOT_PATH") / "nginx"
    if not nginx_path.exists():
        return
    # 配置目录下的Nginx目录
    run_nginx_dir = get_runtime_setting("CONFIG_PATH").with_name("nginx")
    if not run_nginx_dir.exists():
        # 移动到配置目录
        SystemUtils.move(nginx_path, run_nginx_dir)
    # 启动Nginx
    import subprocess

    subprocess.Popen("start nginx.exe", cwd=run_nginx_dir, shell=True)


def stop_frontend():
    """
    停止前端服务
    """
    if not SystemUtils.is_frozen() or not SystemUtils.is_windows():
        return
    import subprocess

    subprocess.Popen("taskkill /f /im nginx.exe", shell=True)


def clear_temp():
    """
    清理临时文件和图片缓存
    """
    # 清理临时目录中3天前的文件
    SystemUtils.clear(
        get_runtime_setting("TEMP_PATH"),
        days=get_runtime_setting("TEMP_FILE_DAYS"),
    )
    # 清理图片缓存目录中7天前的文件
    SystemUtils.clear(
        get_runtime_setting("CACHE_PATH") / "images",
        days=get_runtime_setting("GLOBAL_IMAGE_CACHE_DAYS"),
    )
    # 清理 pip/uv 包下载缓存，不接管整个 .cache 目录。
    clear_package_tool_cache()


def clear_package_tool_cache():
    """
    清理 pip/uv 包下载缓存，只处理 MoviePilot 管理的工具子目录。
    """
    days = get_runtime_setting("PACKAGE_CACHE_DAYS")
    if days <= 0:
        return
    tool_cache_root = get_runtime_setting("PACKAGE_CACHE_PATH")
    for child in ("pip", "uv"):
        cache_path = tool_cache_root / child
        try:
            SystemUtils.clear(cache_path, days=days)
        except Exception as err:
            logger.warning("清理包下载缓存失败：%s - %s", cache_path, err)


def user_auth():
    """
    用户认证检查
    """
    sites_helper = SitesHelper()
    if sites_helper.auth_level >= 2:
        return
    auth_conf = get_configured_system_config().get(SystemConfigKey.UserSiteAuthParams)
    status, msg = sites_helper.check_user(**auth_conf) if auth_conf else sites_helper.check_user()
    if status:
        logger.info(f"{msg} 用户认证成功")
    else:
        logger.info(f"用户认证失败，{msg}")


def check_auth():
    """
    检查认证状态
    """
    if SitesHelper().auth_level < 2:
        err_msg = "用户认证失败，站点相关功能将无法使用！"
        MessageHelper().put(f"注意：{err_msg}", title="用户认证", role="system")
        CommandChain().post_message(
            Message(
                mtype=MessageType.Manual,
                title="MoviePilot用户认证",
                text=err_msg,
                link=get_runtime_setting("MP_DOMAIN")("#/site"),
            )
        )


def update_resources() -> None:
    """安装可用资源更新，并由组合根统一决定是否重启进程。"""
    sites_helper = SitesHelper()
    configure_resource_version_provider(lambda: (sites_helper.auth_version, sites_helper.indexer_version))
    if ResourceHelper().check() is not True:
        return
    restarted, message = SystemHelper.restart()
    if not restarted:
        logger.error(f"资源更新完成但自动重启失败：{message}")


def close_browser_sessions() -> None:
    """在托管资源关闭前释放所有浏览器上下文及其工作线程。"""
    from app.adapters.network.browser import BrowserSessionHelper

    BrowserSessionHelper.close_all_sessions()


async def drain_events() -> bool:
    """在插件卸载前等待已接收事件及其同步、异步处理器完成。"""
    event_manager = EventManager.get_existing_instance()
    if event_manager is None:
        return True
    return await event_manager.drain_async(seal=True)


async def settle_events() -> bool:
    """在插件 handler 停用后结算在途事件，但保留停机 hook 的尾事件入口。"""
    event_manager = EventManager.get_existing_instance()
    if event_manager is None:
        return True
    return await event_manager.drain_async(seal=False)


async def stop_modules() -> bool:
    """
    关闭模块服务，并返回全部资源 owner 是否收敛。
    """
    all_converged = True

    async def run_step(
        name: str,
        callback: Callable[[], object],
        *,
        offload: bool = False,
        record_failure: bool = True,
    ) -> bool:
        """执行单个关闭步骤，失败时继续收口并保留诚实结果。"""
        nonlocal all_converged
        try:
            if offload:
                result = await run_in_threadpool_to_completion(callback)
            else:
                result = callback()
            if inspect.isawaitable(result):
                result = await result
            converged = result is not False
            if not converged:
                logger.error("关闭%s未收敛，继续执行后续资源收口", name)
        except asyncio.CancelledError:
            logger.warning("关闭%s时收到取消请求，继续执行资源收口", name)
            converged = False
        except Exception as err:
            logger.error(f"关闭{name}失败：{err}")
            converged = False
        if not converged and record_failure:
            all_converged = False
        return converged

    await run_step("图片代理安全日志合并器", close_image_proxy_block_log_coalescer)
    await run_step("模块", lambda: ModuleManager().shutdown(), offload=True)
    await run_step("事件消费", lambda: EventManager().stop_async())
    await run_step("浏览器会话", close_browser_sessions, offload=True)
    await run_step("托管资源", stop_managed_resources)
    await run_step("DoH服务", lambda: DohHelper().shutdown(), offload=True)
    await run_step("线程池", lambda: ThreadHelper().shutdown(), offload=True)
    await run_step("Redis缓存连接", lambda: RedisHelper().close(), offload=True)
    await run_step("异步Redis缓存连接", lambda: AsyncRedisHelper().close())
    # Web Agent 的取消 finally 可能还要写入最终展示快照，必须先完成任务收尾，再关闭写入准入。
    web_agent_drained = await run_step(
        "Web Agent后台任务",
        shutdown_web_agent_background_tasks,
        record_failure=False,
    )
    if not web_agent_drained:
        web_agent_drained = await run_step("Web Agent后台任务收尾", wait_web_agent_background_tasks)
    if web_agent_drained:
        await run_step(
            "Agent会话持久化准入",
            lambda: get_configured_agent_chat_persistence().begin_shutdown(),
        )
        persistence_drained = await run_step(
            "Agent会话持久化",
            lambda: get_configured_agent_chat_persistence().shutdown(),
        )
    else:
        persistence_drained = False
        all_converged = False
        logger.error("Web Agent任务未完成收尾，跳过持久化和数据库关闭以保护活动事务")
    if persistence_drained:
        await run_step("数据库任务", stop_database_worker)
        if _database_worker is None:
            await run_step("数据库连接", close_database)
        else:
            all_converged = False
            logger.error("数据库任务未收敛，跳过数据库连接关闭以避免运行中事务使用已释放连接")
    await run_step("前端服务", stop_frontend, offload=True)
    await run_step("临时文件", clear_temp, offload=True)
    return all_converged


async def _initialize_modules() -> HostRuntime:
    """
    构造模块服务并返回本次 lifespan 唯一的类型化 HostRuntime。
    """
    global _database_worker
    configure_application_network_ports()
    # 兼容 Oper 的无 Session 写入口仍由组合根持有事务，避免模型恢复自动提交。
    transaction_runner = TransactionalWriteRunner(
        sync_session=SessionFactory,
        async_session=async_session_scope,
    )
    configure_transaction_runners(
        sync=transaction_runner.sync,
        async_=transaction_runner.async_,
    )
    database_worker = DatabaseWorker()
    await database_worker.start()
    _database_worker = database_worker
    try:
        system_config = await _initialize_configuration_services(database_worker)
    except BaseException:
        try:
            await stop_database_worker()
        except Exception as cleanup_error:  # noqa: BLE001  保留原始启动异常
            logger.error(f"启动失败后的数据库任务清理失败：{cleanup_error}")
        raise
    data_query_adapter = SqlAlchemyDataQueryAdapter(SessionFactory)
    configure_data_query_service(
        DataQueryService(
            subscriptions=data_query_adapter,
            histories=data_query_adapter,
            async_executor=database_worker,
        )
    )
    configure_plugin_persistence(
        PluginPersistenceService(
            executor=database_worker,
            identities=TransactionalPluginIdentityStore(SessionFactory),
            installations=TransactionalPluginInstallationStore(
                SessionFactory,
                system_config.update_atomically,
            ),
        )
    )
    # 数据访问能力统一在启动组合根注入，Runtime 和 Adapter 不再直接依赖 Oper。
    api_data = ApiDataPorts(
        sync_session=get_db,
        async_session=get_async_db,
        repositories={
            "download_history": SessionDownloadHistoryRepository,
            "media_server": MediaServerOper,
            "message": MessageOper,
            "passkey": PassKeyOper,
            "site": SessionSiteRepository,
            "subscribe": SessionSubscriptionRepository,
            "subscribe_history": SessionSubscriptionHistoryRepository,
            "user": SqlAlchemyUserRepository,
            "workflow": WorkflowOper,
        },
        standalone={
            "passkey": PassKeyOper,
            "system_config": SystemConfigOper,
            "user": _build_transactional_user_repository,
        },
        unit_of_work={
            "async": SqlAlchemyAsyncUnitOfWork,
            "sync": SqlAlchemyUnitOfWork,
        },
    )
    runtime_configuration = RuntimeConfiguration(
        api=lambda: build_api_runtime_config(legacy_settings),
        scheduler=lambda: build_scheduler_runtime_config(legacy_settings),
        chain=lambda: build_chain_runtime_config(legacy_settings),
    )
    runtime_settings = _build_runtime_settings_service()
    workflow_query = WorkflowQueryService(
        repository=TransactionalWorkflowQueryRepository(
            sync_session=SessionFactory,
            async_session=async_session_scope,
        )
    )
    configure_workflow_query(workflow_query)
    download_history_repository = TransactionalDownloadHistoryRepository(
        sync_session=SessionFactory,
        async_session=async_session_scope,
    )
    transfer_history_repository = TransactionalTransferHistoryRepository(
        sync_session=SessionFactory,
        async_session=async_session_scope,
    )
    agent_chat_persistence = AgentChatPersistenceService(
        repository=lambda session: AgentChatOper(session),
        async_executor=database_worker,
        sync_transaction=transaction_runner.sync,
        capacity=database_worker.snapshot().capacity,
    )
    site_repository = TransactionalSiteRepository(
        sync_session=SessionFactory,
        async_session=async_session_scope,
    )
    subscription_repository = TransactionalSubscriptionRepository(
        sync_session=SessionFactory,
        async_session=async_session_scope,
    )
    subscription_history_repository = TransactionalSubscriptionHistoryRepository(
        async_session=async_session_scope,
    )
    agent_chat_service = AgentChatService(repository=AgentChatOper())
    agent_task_repository = TransactionalAgentTaskRepository(SessionFactory)
    plugin_data_repository = TransactionalPluginDataRepository(async_session_scope)
    transfer_execution_repository = TransactionalTransferExecutionRepository(
        SessionFactory
    )
    message_helper = MessageHelper()
    message_queue = MessageQueueManager(auto_start=False)
    agent_data = AgentDataContext(
        chat=agent_chat_service,
        chat_persistence=agent_chat_persistence,
        tasks=agent_task_repository,
        users=_build_transactional_user_repository(),
        sites=site_repository,
        subscriptions=subscription_repository,
        subscription_mutation_scope=subscription_mutation_scope,
        subscription_delete_scope=delete_subscribe_scope,
        async_rule_group_mutation_scope=partial(
            async_rule_group_mutation_scope,
            system_config.publish_many,
        ),
        subscription_history=subscription_history_repository,
        transfer_history=transfer_history_repository,
        transfer_execution=transfer_execution_repository,
        download_history=download_history_repository,
        plugin_data=plugin_data_repository,
    )
    host_runtime = HostRuntime(
        agent_chat=AgentChatRuntime(
            async_session=get_async_db,
            repository=AgentChatOper,
            transaction=SqlAlchemyAsyncUnitOfWork,
            persistence=agent_chat_persistence,
        ),
        agent=agent_data,
        persistence=PersistenceRuntime(
            sync_session=get_db,
            async_session=get_async_db,
            sync_transaction=SqlAlchemyUnitOfWork,
            async_transaction=SqlAlchemyAsyncUnitOfWork,
        ),
        authentication=AuthenticationRuntime(
            user_repository=SqlAlchemyUserRepository,
            standalone_user=_build_transactional_user_repository,
            system_config=SystemConfigOper,
            passkey=PassKeyOper,
        ),
        messaging=MessagingRuntime(
            repository=MessageOper,
            helper=message_helper,
            queue=message_queue,
        ),
        history=HistoryRuntime(
            download_repository=SessionDownloadHistoryRepository,
            transfer_repository=transfer_history_repository,
            transfer_mutation_repository=SessionTransferHistoryRepository,
            media_server_repository=MediaServerOper,
            transfer_execution_repository=transfer_execution_repository,
        ),
        site=SiteRuntime(
            repository=SessionSiteRepository,
            standalone=site_repository,
        ),
        subscription=SubscriptionRuntime(
            async_session=get_async_db,
            repository=SessionSubscriptionRepository,
            history_repository=SessionSubscriptionHistoryRepository,
            transaction=SqlAlchemyAsyncUnitOfWork,
            outbox=SqlAlchemyAsyncOutboxStager,
            dispatch_store=SqlAlchemyAsyncOutboxDispatchStore(async_session_scope),
            batch_writer=build_subscription_batch_writer,
            rule_group_mutation_scope=partial(
                rule_group_mutation_scope,
                system_config.publish_many,
            ),
            async_rule_group_mutation_scope=partial(
                async_rule_group_mutation_scope,
                system_config.publish_many,
            ),
            site_reference_mutation_scope=partial(
                site_reference_mutation_scope,
                system_config.publish_many,
            ),
        ),
        workflow=WorkflowRuntime(
            query=workflow_query,
            repository=WorkflowOper,
            system_config=get_configured_system_config,
        ),
        configuration=runtime_configuration,
        settings=runtime_settings,
        tasks=get_task_registry(),
    )
    configure_runtime_configuration(host_runtime.configuration)
    configure_runtime_settings(host_runtime.settings)
    configure_runtime_setting_provider(lambda key: getattr(legacy_settings, key))
    configure_runtime_setting_updater(host_runtime.settings.update)
    configure_token_runtime_config(lambda: build_token_runtime_config(legacy_settings))
    # 旧 app.api.data 导入只保留 ABI 转发，正式 API 依赖全部读取 HostRuntime。
    configure_api_data_runtime(api_data)
    configure_runtime_data_providers(workflow_query, subscription_repository)
    workflow_execution = TransactionalWorkflowExecutionService(SessionFactory)
    configure_workflow_execution(workflow_execution)
    configure_outbox_dispatcher(_build_outbox_dispatcher)
    configure_transfer_retry_config(
        lambda: TransferRetryConfig(
            max_failed_retries=get_runtime_setting("TRANSFER_MAX_FAILED_RETRIES"),
        )
    )
    configure_database_governance(build_database_governance())
    configure_agent_chat_service(agent_chat_service)
    configure_agent_chat_persistence(agent_chat_persistence)
    configure_user_lookups(
        by_id=lambda user_id: _build_transactional_user_repository().get_by_id(user_id),
        by_name=lambda username: _build_transactional_user_repository().get_by_name(username),
        by_channel=lambda **bindings: _build_transactional_user_repository().find_name_by_bindings(bindings),
    )
    configure_auth_service(
        AuthService(
            users=_build_transactional_user_repository(),
            config=get_configured_system_config(),
            passkeys=PassKeyOper(),
        )
    )
    configure_passkey_challenge_cache(
        TTLCache(
            region="passkey_challenge",
            maxsize=4096,
            ttl=PASSKEY_CHALLENGE_TTL_SECONDS,
        )
    )
    configure_passkey_service(PasskeyService(repository=PassKeyOper()))
    configure_transfer_history_repository(lambda: transfer_history_repository)
    configure_site_query_service(SiteQueryService(repository=site_repository))
    configure_site_health_service(SiteHealthService(repository=site_repository))
    configure_agent_data_context(agent_data)
    from app.agent.tools.manager import moviepilot_tool_manager

    moviepilot_tool_manager.set_data_context(agent_data)
    configure_scheduler_agent_tasks(agent_task_repository)
    configure_agent_task_execution(
        AgentTaskExecutionService(
            repository=SessionAgentTaskRepository,
            async_executor=database_worker,
            sync_transaction=transaction_runner.sync,
        )
    )
    # 托管资源只在这里装配声明与 adapter，具体资源仍由首个消费者显式激活。
    init_managed_resources()
    # 应用服务不反向依赖 Chain，由启动组合层注入壁纸来源。
    configure_wallpaper_services()
    # Chain 无参兼容入口由组合根明确提供依赖上下文；测试和新代码可直接注入替代上下文。
    configure_chain_runtime_context_provider(
        lambda: _build_chain_runtime_context(
            message_helper=message_helper,
            message_queue=message_queue,
            system_config=system_config,
            site=site_repository,
            subscription=subscription_repository,
            download_history=download_history_repository,
            transfer_history=transfer_history_repository,
        )
    )
    # 认证访问层不反向依赖数据库实现，由启动组合层注入载荷提供器。
    set_superuser_token_payload_provider(build_superuser_token_payload)
    # DoH
    DohHelper()
    # 站点管理
    SitesHelper()
    # 资源适配器只负责下载安装，是否重启由启动组合层决定。
    update_resources()
    # 用户认证
    user_auth()
    # 事件错误通知由启动组合层接入消息服务。
    EventManager().set_error_notifier(notify_event_error)
    # WebAgent 事件监听由组合根统一装配，HTTP 请求只管理自己的队列。
    EventManager().add_event_listener(
        EventType.NoticeMessage,
        dispatch_web_agent_message_event,
    )
    # 宿主类处理器在启动层显式登记，事件总线不再兜底 owner_class()。
    configure_host_event_handler_resolver()
    # 加载模块
    ModuleManager()
    # 启动事件消费
    EventManager().start()
    # 初始化共享服务端状态
    await MoviePilotServerHelper.async_init_plugin_report()
    await MoviePilotServerHelper.async_init_subscribe_report()
    MoviePilotServerHelper.get_user_uuid()
    MoviePilotServerHelper.get_github_user()
    # 初始化AI智能体
    await init_agent()
    # 启动前端服务
    start_frontend()
    # 检查认证状态
    check_auth()
    return host_runtime


async def init_modules() -> HostRuntime:
    """启动模块服务，并在构造中途失败时回收尚未发布的资源 owner。"""
    try:
        return await _initialize_modules()
    except BaseException:
        try:
            if stop_message() is False:
                logger.error("模块启动失败后的消息资源未完全收敛")
        except Exception as cleanup_error:  # noqa: BLE001  保留原始启动异常
            logger.error(f"模块启动失败后的消息资源清理失败：{cleanup_error}")
        try:
            await stop_database_worker()
        except Exception as cleanup_error:  # noqa: BLE001  保留原始启动异常
            logger.error(f"模块启动失败后的数据库任务清理失败：{cleanup_error}")
        raise
