import inspect
import sys
from typing import Callable

from app.adapters.cache.redis import RedisHelper, AsyncRedisHelper
from app.application.orchestration.mediaserver import MediaServerChain
from app.application.orchestration.tmdb import TmdbChain

# SitesHelper涉及资源包拉取，提前引入并容错提示
try:
    from app.application.site.sites import SitesHelper  # noqa  # pylint: disable=import-error,no-name-in-module
except ImportError as e:
    SitesHelper = None
    error_message = f"错误: {str(e)}\n站点认证及索引相关资源导入失败，请尝试重建容器或手动拉取资源"
    print(error_message, file=sys.stderr)
    sys.exit(1)

from app.adapters.system.host import SystemUtils
from app.runtime.log import logger
from app.runtime.config import settings
from app.runtime.extensions.module_manager import ModuleManager
from app.runtime.extensions.plugin_manager import PluginManager
from app.runtime.events import EventHandlerBinding, EventManager
from app.runtime.observability import record_metric
from app.runtime.state import SystemHelper
from app.runtime.thread import ThreadHelper
from app.adapters.network.doh import DohHelper
from app.adapters.system.resource import (
    ResourceHelper,
    configure_resource_version_provider,
)
from app.application.messaging.message import (
    MessageHelper,
    MessageQueueManager,
    stop_message,
)
from app.runtime.cache import AsyncFileCache, FileCache
from app.runtime.extensions.projection.dispatcher import ModuleInvocationDispatcher
from app.application.configuration import (
    RuntimeConfiguration,
    RuntimeSettingsService,
    SystemConfigService,
    TransferRetryConfig,
    configure_runtime_configuration,
    configure_runtime_settings,
    configure_system_config,
    configure_transfer_retry_config,
)
from app.startup.ports.configuration import (
    build_api_runtime_config,
    build_chain_runtime_config,
    build_scheduler_runtime_config,
)
from app.application.database import configure_database_governance
from app.application.plugin.runtime import configure_plugin_runtime
from app.application.module import configure_module_runtime
from app.application.messaging.chat import AgentChatService, configure_agent_chat_service
from app.application.security.user import configure_user_lookups
from app.application.security.auth import AuthService, configure_auth_service
from app.application.security.passkeys import PasskeyService, configure_passkey_service
from app.application.security.userconfig import (
    UserConfigurationService,
    configure_user_configuration,
)
from app.application.history import configure_transfer_history_provider
from app.application.outbox import OutboxDispatcher, configure_outbox_dispatcher
from app.startup.ports.outbox import SqlAlchemyAsyncOutboxStager, SqlAlchemyOutboxRepository
from app.application.site.query import SiteQueryService, configure_site_query_service
from app.application.site.health import SiteHealthService, configure_site_health_service
from app.application.workflow import WorkflowQueryService, configure_workflow_query
from app.application.agentdata import configure_agent_data_ports
from app.api.data import ApiDataPorts, configure_api_data_runtime
from app.application.subscription.write import configure_subscribe_writer
from app.adapters.external.server import (
    MoviePilotServerHelper,
    configure_server_application_services,
)
from app.application.server.report import ServerReportService
from app.application.server.share import ServerSharingService
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
from app.db.oper.serviceconfig import ServiceConfigOper
from app.db.oper.subscribe import SubscribeOper
from app.db.oper.agentchat import AgentChatOper
from app.db.oper.agenttask import AgentTaskOper
from app.db.oper.user import UserOper
from app.db.oper.passkey import PassKeyOper
from app.db.oper.userconfig import UserConfigOper
from app.db.oper.transferhistory import TransferHistoryOper
from app.db.oper.downloadhistory import DownloadHistoryOper
from app.db.oper.transferpending import TransferPendingOper
from app.db.oper.mediaserver import MediaServerOper
from app.db.oper.site import SiteOper
from app.db.oper.message import MessageOper
from app.db.oper.subscribehistory import SubscribeHistoryOper
from app.db.oper.plugindata import PluginDataOper
from app.db.oper.systemconfig import SystemConfigOper
from app.db.oper.workflow import WorkflowOper, configure_workflow_legacy_writer
from app.application.messaging.gateway import CommandChain
from app.schemas.message import Message
from app.schemas.message import MessageType
from app.schemas.types import EventType, SystemConfigKey
from app.startup.agent_initializer import init_agent, stop_agent
from app.startup.bindings.database import build_database_governance
from app.startup.managed_resources_initializer import (
    init_managed_resources,
    stop_managed_resources,
)
from app.startup.ports.subscription import (
    TransactionalSubscribeWriter,
    configure_transactional_subscription_scopes,
)
from app.startup.ports.chain_events import TransactionalChainDurableEventWriter
from app.startup.ports.download_failure import TransactionalDownloadFailureRepository
from app.startup.ports.workflow import TransactionalWorkflowExecutionService
from app.startup.ports.transaction import TransactionalWriteRunner
from app.startup.ports.context import (
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
from app.adapters.web.security.access import set_superuser_token_payload_provider
from app.application.security.auth import build_superuser_token_payload
from app.application.image import configure_wallpaper_providers
from app.application.orchestration.context import (
    ChainRuntimeContext,
    configure_chain_runtime_context_provider,
)
from app.application.orchestration.durable_events import (
    restore_download_added,
    restore_transfer_result,
)
from app.application.orchestration.data import (
    configure_chain_data_ports,
    get_chain_data_ports,
)
from app.application.service_config import (
    ServiceInstanceConfigService,
    configure_service_instance_configs,
    get_configured_service_instance_configs,
)
from app.runtime.extensions.registry.meta_parser import configure_meta_parser_order_reader
from app.runtime.extensions.service_config import (
    configure_service_config_reader,
    configure_service_instance_config_reader,
)
from app.startup.hostport_initializer import (
    configure_dispatch_host_ports,
    configure_host_ports,
)


def build_default_chain_runtime_context() -> ChainRuntimeContext:
    """
    按宿主既有的管理器单例身份组装 Chain 运行上下文

    上下文本身只声明所需对象，装配这些全局管理器是组合根的职责，因此构造放在
    这里而不是声明处，避免应用层在模块导入期就抓取运行时管理器与数据库操作器。
    :return: Chain 无参兼容入口使用的运行上下文
    """
    return ChainRuntimeContext(
        module_manager=ModuleManager(),
        plugin_manager=PluginManager(),
        event_manager=EventManager(),
        message_oper=MessageOper(),
        message_helper=MessageHelper(),
        file_cache=FileCache(),
        async_file_cache=AsyncFileCache(),
        message_queue_factory=lambda callback: MessageQueueManager(send_callback=callback),
        module_dispatcher_factory=ModuleInvocationDispatcher,
        configuration=build_chain_runtime_config(settings),
        data_ports=get_chain_data_ports(),
        durable_event_writer=TransactionalChainDurableEventWriter(SessionFactory),
    )


async def _async_get_subscribe(subscribe_id: int):
    """通过数据库操作器异步读取订阅，供服务端共享用例使用。"""
    return await SubscribeOper().async_get(subscribe_id)


async def _async_get_workflow(workflow_id: int):
    """通过数据库操作器异步读取工作流，供服务端共享用例使用。"""
    return await WorkflowOper().async_get(workflow_id)


def configure_runtime_data_providers() -> None:
    """在启动组合层装配运行时和外部服务所需的数据库读取能力。"""
    configure_service_config_reader(lambda key: SystemConfigOper().get(key))
    configure_service_instance_configs(
        ServiceInstanceConfigService(repository=ServiceConfigOper())
    )
    configure_service_instance_config_reader(
        lambda capability: get_configured_service_instance_configs().read(capability)
    )
    configure_meta_parser_order_reader(
        lambda: SystemConfigOper().get(SystemConfigKey.MetaParserOrder)
    )
    configure_server_application_services(
        report_service=ServerReportService(
            config_reader=lambda key: SystemConfigOper().get(key),
            config_writer=lambda key, value: SystemConfigOper().set(key, value),
            installed_plugins_provider=lambda: SystemConfigOper().get(
                SystemConfigKey.UserInstalledPlugins
            ) or [],
            subscribes_provider=lambda: SubscribeOper().list(),
            plugin_report_sender=MoviePilotServerHelper.plugin_install_report,
            async_plugin_report_sender=(
                MoviePilotServerHelper.async_plugin_install_report
            ),
            subscribe_report_sender=MoviePilotServerHelper.subscribe_report,
            repo_url_sanitizer=MoviePilotServerHelper.sanitize_plugin_repo_url,
        ),
        sharing_service=ServerSharingService(
            subscribe_provider=lambda subscribe_id: SubscribeOper().get(
                subscribe_id
            ),
            async_subscribe_provider=_async_get_subscribe,
            workflow_provider=lambda workflow_id: WorkflowOper().get(workflow_id),
            async_workflow_provider=_async_get_workflow,
            user_uuid_provider=MoviePilotServerHelper.get_user_uuid,
            subscribe_sender=MoviePilotServerHelper.subscribe_share,
            async_subscribe_sender=MoviePilotServerHelper.async_subscribe_share,
            workflow_sender=MoviePilotServerHelper.workflow_share,
            async_workflow_sender=MoviePilotServerHelper.async_workflow_share,
            response_handler=MoviePilotServerHelper._handle_response,
            subscribe_cache_clearer=(
                MoviePilotServerHelper._clear_subscribe_share_cache
            ),
            workflow_cache_clearer=(
                MoviePilotServerHelper._clear_workflow_share_cache
            ),
        ),
    )


def _build_outbox_dispatcher() -> OutboxDispatcher:
    """创建一次恢复批次独占的 Session、Repository 和事件 handler。"""
    session = SessionFactory()
    return OutboxDispatcher(
        repository=SqlAlchemyOutboxRepository(session),
        handlers={
            "subscribe.added": lambda message: EventManager().send_event(
                EventType.SubscribeAdded,
                message.payload,
            ),
            "subscribe.modified": lambda message: EventManager().send_event(
                EventType.SubscribeModified,
                message.payload,
            ),
            "subscribe.deleted": lambda message: EventManager().send_event(
                EventType.SubscribeDeleted,
                message.payload,
            ),
            "download.added": lambda message: EventManager().send_event(
                EventType.DownloadAdded,
                restore_download_added(message.payload),
            ),
            "transfer.completed": lambda message: EventManager().send_event(
                EventType.TransferComplete,
                restore_transfer_result(message.payload),
            ),
            "transfer.failed": lambda message: EventManager().send_event(
                EventType.TransferFailed,
                restore_transfer_result(message.payload),
            ),
        },
        close=session.close,
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
        mediaserver_wallpapers=lambda count: MediaServerChain().get_latest_wallpapers(
            count=count
        ),
    )


def notify_event_error(title: str, message: str) -> None:
    """将事件总线错误转发到系统消息通道。"""
    MessageHelper().put(
        title=title,
        message=message,
        role="system",
    )


def get_host_event_handler_factories() -> dict[type, Callable[[], object]]:
    """返回所有使用事件装饰器的宿主类及其明确实例工厂。"""
    from app.application.orchestration.download import DownloadChain
    from app.application.orchestration.scraping import ScrapingChain
    from app.application.orchestration.search import SearchChain
    from app.application.orchestration.site import SiteChain
    from app.application.orchestration.subscribe import SubscribeChain
    from app.workflow.service import WorkflowChain
    from app.runtime.command import Command
    from app.scheduler import PluginScheduling, Scheduler

    return {
        Command: Command,
        DownloadChain: DownloadChain,
        # 插件重载处理器声明在插件调度混入类上，实例仍是调度器组合根单例
        PluginScheduling: Scheduler,
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

    def resolve(owner_class: type) -> list[EventHandlerBinding] | None:
        """按明确白名单复用单例或构造与旧路径等价的 Chain 实例绑定列表。"""
        factory = factories.get(owner_class)
        if factory is None:
            return None
        get_existing = getattr(owner_class, "get_existing_instance", None)
        instance = get_existing() if callable(get_existing) else None
        if instance is None:
            instance = factory()
        return [
            EventHandlerBinding(
                instance=instance,
                owner_name=owner_class.__name__,
            )
        ]

    EventManager().register_handler_instance_resolver("host", resolve)


def start_frontend():
    """
    启动前端服务
    """
    # 仅Windows可执行文件支持内嵌nginx
    if not SystemUtils.is_frozen() \
            or not SystemUtils.is_windows():
        return
    # 临时Nginx目录
    nginx_path = settings.ROOT_PATH / 'nginx'
    if not nginx_path.exists():
        return
    # 配置目录下的Nginx目录
    run_nginx_dir = settings.CONFIG_PATH.with_name('nginx')
    if not run_nginx_dir.exists():
        # 移动到配置目录
        SystemUtils.move(nginx_path, run_nginx_dir)
    # 启动Nginx
    import subprocess
    subprocess.Popen("start nginx.exe",
                     cwd=run_nginx_dir,
                     shell=True)


def stop_frontend():
    """
    停止前端服务
    """
    if not SystemUtils.is_frozen() \
            or not SystemUtils.is_windows():
        return
    import subprocess
    subprocess.Popen(f"taskkill /f /im nginx.exe", shell=True)


def clear_temp():
    """
    清理临时文件和图片缓存
    """
    # 清理临时目录中3天前的文件
    SystemUtils.clear(settings.TEMP_PATH, days=settings.TEMP_FILE_DAYS)
    # 清理图片缓存目录中7天前的文件
    SystemUtils.clear(settings.CACHE_PATH / "images", days=settings.GLOBAL_IMAGE_CACHE_DAYS)
    # 清理 pip/uv 包下载缓存，不接管整个 .cache 目录。
    clear_package_tool_cache()


def clear_package_tool_cache():
    """
    清理 pip/uv 包下载缓存，只处理 MoviePilot 管理的工具子目录。
    """
    days = settings.PACKAGE_CACHE_DAYS
    if days <= 0:
        return
    tool_cache_root = settings.PACKAGE_CACHE_PATH
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
    auth_conf = SystemConfigOper().get(SystemConfigKey.UserSiteAuthParams)
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
                link=settings.MP_DOMAIN('#/site')
            )
        )


def update_resources() -> None:
    """安装可用资源更新，并由组合根统一决定是否重启进程。"""
    sites_helper = SitesHelper()
    configure_resource_version_provider(
        lambda: (sites_helper.auth_version, sites_helper.indexer_version)
    )
    if ResourceHelper().check() is not True:
        return
    restarted, message = SystemHelper.restart()
    if not restarted:
        logger.error(f"资源更新完成但自动重启失败：{message}")


def close_browser_sessions() -> None:
    """在托管资源关闭前释放所有浏览器上下文及其工作线程。"""
    from app.adapters.network.browser import BrowserSessionHelper

    BrowserSessionHelper.close_all_sessions()


async def stop_modules():
    """
    服务关闭
    """
    async def run_step(name: str, callback: Callable[[], object]) -> None:
        """单个模块资源关闭失败时继续执行后续阶段"""
        try:
            result = callback()
            if inspect.isawaitable(result):
                await result
        except Exception as err:
            logger.error(f"关闭{name}失败：{err}")

    await run_step("AI智能体", stop_agent)
    await run_step("模块", lambda: ModuleManager().shutdown())
    await run_step("事件消费", lambda: EventManager().stop())
    await run_step("浏览器会话", close_browser_sessions)
    await run_step("托管资源", stop_managed_resources)
    await run_step("DoH服务", lambda: DohHelper().shutdown())
    await run_step("线程池", lambda: ThreadHelper().shutdown())
    await run_step("消息服务", stop_message)
    await run_step("Redis缓存连接", lambda: RedisHelper().close())
    await run_step("异步Redis缓存连接", lambda: AsyncRedisHelper().close())
    await run_step("数据库连接", close_database)
    await run_step("前端服务", stop_frontend)
    await run_step("临时文件", clear_temp)


async def init_modules() -> HostRuntime:
    """
    启动模块并返回本次 lifespan 唯一的类型化 HostRuntime。
    """
    # 扩展经端口取用目录、存储、命名、站点资源与规则配置，须先于模块加载完成注入。
    configure_host_ports()
    # 入口层经应用端口取用模块目录与插件目录，不直接构造运行时单例。
    configure_module_runtime(lambda: ModuleManager())
    configure_plugin_runtime(lambda: PluginManager())
    # 兼容 Oper 的无 Session 写入口仍由组合根持有事务，避免模型恢复自动提交。
    transaction_runner = TransactionalWriteRunner(
        sync_session=SessionFactory,
        async_session=async_session_scope,
    )
    configure_transaction_runners(
        sync=transaction_runner.sync,
        async_=transaction_runner.async_,
    )
    # 数据访问能力统一在启动组合根注入，Runtime 和 Adapter 不再直接依赖 Oper。
    api_data = ApiDataPorts(
        sync_session=get_db,
        async_session=get_async_db,
        repositories={
            "download_history": DownloadHistoryOper,
            "media_server": MediaServerOper,
            "message": MessageOper,
            "passkey": PassKeyOper,
            "site": SiteOper,
            "subscribe": SubscribeOper,
            "subscribe_history": SubscribeHistoryOper,
            "transfer_history": TransferHistoryOper,
            "user": UserOper,
            "workflow": WorkflowOper,
        },
        standalone={
            "passkey": PassKeyOper,
            "system_config": SystemConfigOper,
            "user": UserOper,
        },
        unit_of_work={
            "async": SqlAlchemyAsyncUnitOfWork,
            "sync": SqlAlchemyUnitOfWork,
        },
    )
    runtime_configuration = RuntimeConfiguration(
        api=lambda: build_api_runtime_config(settings),
        scheduler=lambda: build_scheduler_runtime_config(settings),
        chain=lambda: build_chain_runtime_config(settings),
    )
    runtime_settings = RuntimeSettingsService(settings)
    host_runtime = HostRuntime(
        agent_chat=AgentChatRuntime(
            async_session=get_async_db,
            repository=AgentChatOper,
            transaction=SqlAlchemyAsyncUnitOfWork,
        ),
        persistence=PersistenceRuntime(
            sync_session=get_db,
            async_session=get_async_db,
            sync_transaction=SqlAlchemyUnitOfWork,
            async_transaction=SqlAlchemyAsyncUnitOfWork,
        ),
        authentication=AuthenticationRuntime(
            user_repository=UserOper,
            standalone_user=UserOper,
            system_config=SystemConfigOper,
            passkey=PassKeyOper,
        ),
        messaging=MessagingRuntime(repository=MessageOper),
        history=HistoryRuntime(
            download_repository=DownloadHistoryOper,
            transfer_repository=TransferHistoryOper,
            media_server_repository=MediaServerOper,
        ),
        site=SiteRuntime(repository=SiteOper),
        subscription=SubscriptionRuntime(
            async_session=get_async_db,
            repository=SubscribeOper,
            history_repository=SubscribeHistoryOper,
            transaction=SqlAlchemyAsyncUnitOfWork,
            outbox=SqlAlchemyAsyncOutboxStager,
        ),
        workflow=WorkflowRuntime(
            repository=WorkflowOper,
            system_config=SystemConfigOper,
        ),
        configuration=runtime_configuration,
        settings=runtime_settings,
    )
    configure_runtime_configuration(host_runtime.configuration)
    configure_runtime_settings(host_runtime.settings)
    # 旧 app.api.data 导入只保留 ABI 转发，正式 API 依赖全部读取 HostRuntime。
    configure_api_data_runtime(api_data)
    configure_runtime_data_providers()
    workflow_execution = TransactionalWorkflowExecutionService(SessionFactory)
    configure_workflow_legacy_writer(workflow_execution)
    configure_chain_data_ports(
        site=lambda: SiteOper(),
        subscribe=lambda: SubscribeOper(),
        workflow=lambda: WorkflowOper(),
        download_history=lambda: DownloadHistoryOper(),
        transfer_history=lambda: TransferHistoryOper(),
        transfer_pending=lambda: TransferPendingOper(),
        media_server=lambda: MediaServerOper(),
        download_failure=lambda: TransactionalDownloadFailureRepository(
            SessionFactory
        ),
        user=lambda: UserOper(),
    )
    configure_system_config(SystemConfigService(repository=SystemConfigOper()))
    configure_outbox_dispatcher(_build_outbox_dispatcher)
    configure_transfer_retry_config(
        lambda: TransferRetryConfig(
            max_failed_retries=settings.TRANSFER_MAX_FAILED_RETRIES,
        )
    )
    configure_database_governance(build_database_governance())
    configure_agent_chat_service(AgentChatService(repository=AgentChatOper()))
    configure_user_lookups(
        by_id=lambda user_id: UserOper().get_by_id(user_id),
        by_name=lambda username: UserOper().get_by_name(username),
        by_channel=lambda **bindings: UserOper().get_name(**bindings),
    )
    configure_auth_service(
        AuthService(
            users=UserOper(),
            config=SystemConfigOper(),
            passkeys=PassKeyOper(),
        )
    )
    configure_passkey_service(PasskeyService(repository=PassKeyOper()))
    configure_user_configuration(UserConfigurationService(repository=UserConfigOper()))
    configure_transfer_history_provider(lambda: TransferHistoryOper())
    configure_site_query_service(SiteQueryService(repository=SiteOper()))
    configure_site_health_service(SiteHealthService(repository=SiteOper()))
    configure_workflow_query(WorkflowQueryService(repository=WorkflowOper()))
    configure_agent_data_ports(
        agent_chat=lambda: AgentChatOper(),
        agent_task=lambda: AgentTaskOper(),
        user=lambda: UserOper(),
        site=lambda: SiteOper(),
        subscribe=lambda: SubscribeOper(),
        subscribe_history=lambda: SubscribeHistoryOper(),
        transfer_history=lambda: TransferHistoryOper(),
        download_history=lambda: DownloadHistoryOper(),
        workflow=lambda: WorkflowOper(),
        plugin_data=lambda: PluginDataOper(),
    )
    configure_subscribe_writer(
        lambda: TransactionalSubscribeWriter(
            sync_session=SessionFactory,
            async_session=async_session_scope,
        )
    )
    configure_transactional_subscription_scopes()
    # 托管资源只在这里装配声明与 adapter，具体资源仍由首个消费者显式激活。
    init_managed_resources()
    # 应用服务不反向依赖 Chain，由启动组合层注入壁纸来源。
    configure_wallpaper_services()
    # Chain 无参兼容入口由组合根明确提供依赖上下文；测试和新代码可直接注入替代上下文。
    configure_chain_runtime_context_provider(build_default_chain_runtime_context)
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
    # 宿主类处理器在启动层显式登记，事件总线不再兜底 owner_class()。
    configure_host_event_handler_resolver()
    # 加载模块
    ModuleManager()
    # 需要模块分发的扩展端口在模块目录就绪后注册。
    configure_dispatch_host_ports()
    # 启动事件消费
    EventManager().start()
    # 初始化共享服务端状态
    MoviePilotServerHelper.init_plugin_report()
    MoviePilotServerHelper.init_subscribe_report()
    MoviePilotServerHelper.get_user_uuid()
    MoviePilotServerHelper.get_github_user()
    # LLM 提供商管理动作（测试连接、模型目录查询）依赖的构建能力独立于 Agent 启用开关，
    # 须在此无条件注入，使用户在开启智能助手前也能测试模型连接。
    from app.agent.llm import LLMHelper
    from app.agent.llm.provider import configure_llm_operations

    configure_llm_operations(LLMHelper())
    # 初始化AI智能体
    await init_agent()
    # 启动前端服务
    start_frontend()
    # 检查认证状态
    check_auth()
    return host_runtime
