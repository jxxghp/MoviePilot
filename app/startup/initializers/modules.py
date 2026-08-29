import asyncio
import inspect
import sys
from collections.abc import Awaitable
from functools import partial
from typing import Any, Callable, cast

from app.adapters.cache.redis import AsyncRedisHelper, RedisHelper
from app.chain.mediaserver import MediaServerChain
from app.chain.tmdb import TmdbChain

# SitesHelper涉及资源包拉取，提前引入并容错提示
try:
    from app.application.site.sites import SitesHelper  # noqa  # pylint: disable=import-error,no-name-in-module
except ImportError as e:
    SitesHelper = None
    error_message = f"错误: {str(e)}\n站点认证及索引相关资源导入失败，请尝试重建容器或手动拉取资源"
    print(error_message, file=sys.stderr)
    sys.exit(1)

from app.adapters.external.server import MoviePilotServerHelper
from app.adapters.network.doh import DohHelper
from app.adapters.system.host import SystemUtils
from app.adapters.system.resource import (
    ResourceHelper,
    configure_resource_version_provider,
)
from app.application.chain.context import (
    ChainRuntimeContext,
    configure_chain_runtime_context_provider,
)
from app.application.configuration import (
    TransferRetryConfig,
    configure_transfer_retry_config,
    get_configured_system_config,
)
from app.application.history import configure_transfer_history_repository
from app.application.image import configure_wallpaper_providers
from app.application.messaging.agent import (
    dispatch_web_agent_message_event,
    shutdown_web_agent_background_tasks,
    wait_web_agent_background_tasks,
)
from app.application.messaging.chat import (
    get_configured_agent_chat_persistence,
)
from app.application.messaging.message import (
    MessageHelper,
    MessageQueueManager,
    stop_message,
)
from app.application.module import configure_module_runtime
from app.application.outbox import configure_outbox_dispatcher
from app.application.security.url import close_image_proxy_block_log_coalescer
from app.application.service import configure_service_directory
from app.application.site.health import SiteHealthService, configure_site_health_service
from app.application.site.query import SiteQueryService, configure_site_query_service
from app.application.workflow import configure_workflow_execution
from app.command import CommandChain
from app.db.adapters.chain import TransactionalChainDurableEventWriter
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
)
from app.db.adapters.site import SessionSiteRepository, TransactionalSiteRepository
from app.db.adapters.subscription import (
    SessionSubscriptionHistoryRepository,
    SessionSubscriptionRepository,
    TransactionalSubscriptionHistoryRepository,
    TransactionalSubscriptionRepository,
)
from app.db.adapters.transfer.admission import TransactionalTransferAdmissionRepository
from app.db.adapters.transfer.execution import (
    TransactionalTransferExecutionRepository,
)
from app.db.adapters.workflow import (
    TransactionalWorkflowExecutionService,
)
from app.db.oper.mediaserver import MediaServerOper
from app.db.oper.message import MessageOper
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
)
from app.runtime.cache import AsyncFileCache, FileCache
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
from app.runtime.settings import (
    get_runtime_setting,
)
from app.runtime.state import SystemHelper
from app.runtime.stop import runtime_stop_state
from app.runtime.tasks import get_task_registry
from app.runtime.thread import ThreadHelper
from app.schemas.message import Message, MessageType
from app.schemas.types import EventType, SystemConfigKey
from app.startup.composition.agent import (
    compose_agent,
    publish_agent_services,
)
from app.startup.composition.configuration import (
    build_chain_runtime_config,
    compose_configuration,
    publish_configuration,
    reset_configuration,
)
from app.startup.composition.context import (
    HistoryRuntime,
    HostRuntime,
    MessagingRuntime,
    PersistenceRuntime,
    SiteRuntime,
    SubscriptionRuntime,
    WorkflowRuntime,
)
from app.startup.composition.database import (
    build_transactional_user_repository,
    compose_database_services,
    configure_database,
    database_runtime_active,
    publish_database_services,
    reset_database_services,
    start_database_runtime,
    stop_database_runtime,
)
from app.startup.composition.network import configure_application_network_ports
from app.startup.composition.outbox import build_outbox_dispatcher
from app.startup.composition.security import (
    configure_security_access,
    configure_security_services,
)
from app.startup.composition.server import configure_server_services
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
        user_repository=build_transactional_user_repository(),
        legacy_transfer_command=_execute_legacy_transfer_command,
        configuration=build_chain_runtime_config(legacy_settings),
        durable_event_writer=TransactionalChainDurableEventWriter(SessionFactory),
        stop_state=runtime_stop_state,
    )


def configure_runtime_data_providers() -> None:
    """在启动组合层装配模块和配置目录的运行时读取能力。"""
    configure_service_config_reader(lambda key: get_configured_system_config().get(key))
    configure_module_runtime(lambda: ModuleManager())
    configure_service_directory(
        configs=ServiceConfigHelper.get_configs,
        modules=lambda module_type: ModuleManager().get_running_type_modules(module_type),
    )


def configure_wallpaper_services() -> None:
    """把需要 Chain 编排的壁纸来源注入图片服务。"""
    configure_wallpaper_providers(
        tmdb_wallpaper=lambda: TmdbChain().get_random_wallpager(),
        tmdb_wallpapers=lambda count: TmdbChain().get_trending_wallpapers(count),
        mediaserver_wallpaper=lambda: MediaServerChain().get_latest_wallpaper(),
        mediaserver_wallpapers=lambda count: MediaServerChain().get_latest_wallpapers(count=count),
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
    from app.chain.download import DownloadChain
    from app.chain.scraping import ScrapingChain
    from app.chain.search import SearchChain  # pylint: disable=no-name-in-module
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
        await run_step("数据库任务", stop_database_runtime)
        if not database_runtime_active():
            await run_step("数据库服务", reset_database_services)
            await run_step("配置服务", reset_configuration)
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
    configure_application_network_ports()
    database_runtime = await start_database_runtime()
    try:
        configuration = await compose_configuration(
            executor=database_runtime.worker,
            settings=legacy_settings,
        )
    except BaseException:
        try:
            await stop_database_runtime()
        except Exception as cleanup_error:  # noqa: BLE001  保留原始启动异常
            logger.error(f"启动失败后的数据库任务清理失败：{cleanup_error}")
        raise
    publish_configuration(configuration, legacy_settings)
    database_services = compose_database_services(
        runtime=database_runtime,
        system_config=configuration.system_config,
    )
    system_config = configuration.system_config
    workflow_query = database_services.workflow_query
    download_history_repository = TransactionalDownloadHistoryRepository(
        sync_session=SessionFactory,
        async_session=async_session_scope,
    )
    transfer_history_repository = TransactionalTransferHistoryRepository(
        sync_session=SessionFactory,
        async_session=async_session_scope,
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
    transfer_execution_repository = TransactionalTransferExecutionRepository(SessionFactory)
    message_helper = MessageHelper()
    message_queue = MessageQueueManager(auto_start=False)
    agent_composition = compose_agent(
        runtime=database_runtime,
        system_config=system_config,
        site=site_repository,
        subscription=subscription_repository,
        subscription_history=subscription_history_repository,
        transfer_history=transfer_history_repository,
        transfer_execution=transfer_execution_repository,
        download_history=download_history_repository,
    )
    authentication = configure_security_services()
    host_runtime = HostRuntime(
        agent_chat=agent_composition.chat,
        agent=agent_composition.data,
        persistence=PersistenceRuntime(
            sync_session=get_db,
            async_session=get_async_db,
            sync_transaction=SqlAlchemyUnitOfWork,
            async_transaction=SqlAlchemyAsyncUnitOfWork,
        ),
        authentication=authentication,
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
        configuration=configuration.runtime,
        settings=configuration.settings,
        tasks=get_task_registry(),
    )
    publish_database_services(database_services)
    configure_runtime_data_providers()
    configure_server_services(workflow_query, subscription_repository)
    workflow_execution = TransactionalWorkflowExecutionService(SessionFactory)
    configure_workflow_execution(workflow_execution)
    configure_outbox_dispatcher(build_outbox_dispatcher)
    configure_transfer_retry_config(
        lambda: TransferRetryConfig(
            max_failed_retries=get_runtime_setting("TRANSFER_MAX_FAILED_RETRIES"),
        )
    )
    configure_database()
    publish_agent_services(
        agent_composition,
        data_context_registrar=configure_agent_data_context,
    )
    configure_transfer_history_repository(lambda: transfer_history_repository)
    configure_site_query_service(SiteQueryService(repository=site_repository))
    configure_site_health_service(SiteHealthService(repository=site_repository))
    from app.agent.tools.manager import moviepilot_tool_manager

    moviepilot_tool_manager.set_data_context(agent_composition.data)
    configure_scheduler_agent_tasks(agent_composition.tasks)
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
    configure_security_access()
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
            await stop_database_runtime()
        except Exception as cleanup_error:  # noqa: BLE001  保留原始启动异常
            logger.error(f"模块启动失败后的数据库任务清理失败：{cleanup_error}")
        if not database_runtime_active():
            reset_database_services()
            reset_configuration()
            try:
                await cast(Callable[[], Awaitable[None]], close_database)()
            except Exception as cleanup_error:  # noqa: BLE001  保留原始启动异常
                logger.error(f"模块启动失败后的数据库连接清理失败：{cleanup_error}")
        raise
