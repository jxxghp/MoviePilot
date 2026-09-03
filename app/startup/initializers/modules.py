import asyncio
import inspect
import sys
from typing import Callable, cast

from app.adapters.cache.redis import AsyncRedisHelper, RedisHelper

# SitesHelper涉及资源包拉取，提前引入并容错提示
try:
    from app.application.site.sites import SitesHelper  # noqa  # pylint: disable=import-error,no-name-in-module
except ImportError as e:
    SitesHelper = None
    error_message = f"错误: {str(e)}\n站点认证及索引相关资源导入失败，请尝试重建容器或手动拉取资源"
    print(error_message, file=sys.stderr)
    sys.exit(1)

from app.adapters.external.server import MoviePilotServerHelper
from app.adapters.system.host import SystemUtils
from app.application.configuration import (
    TransferRetryConfig,
    configure_transfer_retry_config,
    get_configured_system_config,
    reset_transfer_retry_config,
)
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
    stop_message,
)
from app.application.module import configure_module_runtime, reset_module_runtime
from app.application.outbox import configure_outbox_dispatcher
from app.application.plugin.runtime import get_existing_plugin_manager
from app.application.security.url import close_image_proxy_block_log_coalescer
from app.application.service import configure_service_directory, reset_service_directory
from app.command import CommandChain
from app.db.session import (
    close_database,
)
from app.runtime.config import settings as legacy_settings
from app.runtime.events import EventHandlerBinding, EventManager
from app.runtime.execution import run_in_threadpool_to_completion
from app.runtime.extensions.module.manager import ModuleManager
from app.runtime.extensions.service import (
    ServiceConfigHelper,
    configure_service_config_reader,
    reset_service_config_reader,
)
from app.runtime.log import logger
from app.runtime.settings import (
    get_runtime_setting,
)
from app.runtime.tasks import get_task_registry
from app.runtime.thread import ThreadHelper
from app.schemas.category import ClassificationFactValue, ClassificationFieldDefinition
from app.schemas.message import Message, MessageType
from app.schemas.types import EventType, SystemConfigKey
from app.startup.composition.agent import (
    compose_agent,
    publish_agent_services,
    reset_agent_services,
)
from app.startup.composition.chain import (
    configure_chain_runtime_context,
    configure_directory_classification_service,
    configure_wallpaper_services,
    reset_chain_services,
)
from app.startup.composition.classification import compose_classification
from app.startup.composition.configuration import (
    compose_configuration,
    publish_configuration,
    reset_configuration,
)
from app.startup.composition.context import HostRuntime
from app.startup.composition.database import (
    compose_database_services,
    configure_database,
    configure_workflow_execution_composition,
    database_runtime_active,
    publish_database_services,
    reset_database_services,
    reset_workflow_execution_composition,
    start_database_runtime,
    stop_database_runtime,
)
from app.startup.composition.enrichment import (
    compose_classification_enrichment,
)
from app.startup.composition.network import (
    configure_application_network_ports,
    configure_doh_composition,
    reset_application_network_ports,
    stop_doh_composition,
)
from app.startup.composition.outbox import build_outbox_dispatcher, reset_outbox_services
from app.startup.composition.resource import (
    configure_site_resource_versions,
    reset_site_resource_composition,
)
from app.startup.composition.runtime import (
    RuntimeInputs,
    compose_runtime,
    compose_runtime_dependencies,
    publish_runtime,
    reset_runtime,
)
from app.startup.composition.security import (
    configure_security_access,
    configure_security_services,
    reset_security_access,
    reset_security_services,
)
from app.startup.composition.server import configure_server_services, reset_server_services
from app.startup.initializers.agent import (
    configure_agent_data_context,
    init_agent,
    reset_agent_data_context,
)
from app.startup.initializers.resources import (
    init_managed_resources,
    reset_managed_resources,
    stop_managed_resources,
)
from app.startup.initializers.scheduler import (
    configure_scheduler_agent_tasks,
    reset_scheduler_bindings,
)


def _plugin_classification_fields() -> tuple[ClassificationFieldDefinition, ...]:
    """读取已创建插件运行时的字段快照，模块初始化阶段不提前物化管理器。"""
    manager = get_existing_plugin_manager()
    if manager is None:
        return ()
    try:
        return cast(
            tuple[ClassificationFieldDefinition, ...],
            manager.get_classification_fields(),
        )
    except Exception as error:  # noqa: BLE001  插件字段目录故障不得阻断策略管理
        logger.warning(f"读取插件分类字段目录失败，已忽略动态字段：{error}")
        return ()


def _plugin_classification_facts(
    media: object,
) -> dict[str, dict[str, ClassificationFactValue]]:
    """通过已创建插件运行时校验媒体对象携带的扩展事实。"""
    manager = get_existing_plugin_manager()
    if manager is None:
        return {}
    try:
        return cast(
            dict[str, dict[str, ClassificationFactValue]],
            manager.get_classification_facts(media),
        )
    except Exception as error:  # noqa: BLE001  插件事实不得成为媒体识别硬依赖
        logger.warning(f"读取插件扩展分类事实失败，已按字段缺失继续：{error}")
        return {}


def _existing_module_manager() -> object | None:
    """返回已创建模块管理器；测试替身或未初始化状态按无 provider 处理。"""
    getter = getattr(ModuleManager, "get_existing_instance", None)
    return getter() if callable(getter) else None


def configure_runtime_data_providers() -> None:
    """在启动组合层装配模块和配置目录的运行时读取能力。"""
    configure_service_config_reader(lambda key: get_configured_system_config().get(key))
    configure_module_runtime(lambda: ModuleManager())
    configure_service_directory(
        configs=ServiceConfigHelper.get_configs,
        modules=lambda module_type: ModuleManager().get_running_type_modules(module_type),
    )


def reset_runtime_data_providers() -> None:
    """按发布逆序撤销模块、服务目录和配置读取 Provider。"""
    reset_service_directory()
    reset_module_runtime()
    reset_service_config_reader()


def reset_event_services() -> None:
    """撤销启动组合根登记的事件通知、监听器和宿主 resolver。"""
    event_manager = EventManager.get_existing_instance()
    if event_manager is None:
        return
    event_manager.remove_event_listener(
        EventType.NoticeMessage,
        dispatch_web_agent_message_event,
    )
    event_manager.unregister_handler_instance_resolver("host")
    event_manager.reset_error_notifier()


def reset_tool_services() -> None:
    """撤销工具管理器持有的 Agent 数据上下文与目录快照。"""
    from app.agent.tools.manager import moviepilot_tool_manager

    moviepilot_tool_manager.reset_data_context()


def _module_provider_reset_steps() -> tuple[tuple[str, Callable[[], object]], ...]:
    """返回当前模块 lifespan 的完整 Provider 逆序撤销清单。"""
    return (
        ("事件服务", reset_event_services),
        ("资源版本 Provider", reset_site_resource_composition),
        ("认证访问服务", reset_security_access),
        ("Chain 服务", reset_chain_services),
        ("托管资源引用", reset_managed_resources),
        ("调度器绑定", reset_scheduler_bindings),
        ("Agent 工具服务", reset_tool_services),
        (
            "Agent 服务",
            lambda: reset_agent_services(
                data_context_resetter=reset_agent_data_context,
            ),
        ),
        ("传输重试配置", reset_transfer_retry_config),
        ("Outbox 服务", reset_outbox_services),
        ("工作流执行服务", reset_workflow_execution_composition),
        ("中心服务", reset_server_services),
        ("运行时数据 Provider", reset_runtime_data_providers),
        ("HostRuntime 投影", reset_runtime),
        ("数据库服务", reset_database_services),
        ("认证服务", reset_security_services),
        ("应用网络端口", reset_application_network_ports),
        ("配置服务", reset_configuration),
    )


def reset_module_providers() -> bool:
    """逐项撤销模块 Provider；单项失败不跳过后续 owner，并返回整体结果。"""
    converged = True
    for name, callback in _module_provider_reset_steps():
        try:
            callback()
        except Exception as error:
            logger.error("撤销%s失败：%s", name, error)
            converged = False
    return converged


def _existing_singleton(instance_type: type) -> object | None:
    """读取已经物化的 Singleton，启动失败回滚时禁止反向创建新 owner。"""
    getter = getattr(instance_type, "get_existing_instance", None)
    return getter() if callable(getter) else None


def _call_existing_singleton(instance_type: type, method_name: str) -> object:
    """调用已存在 Singleton 的关闭方法；owner 未创建时视为已经收敛。"""
    instance = _existing_singleton(instance_type)
    if instance is None:
        return True
    return getattr(instance, method_name)()


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
    from app.chain.search.facade import SearchChain
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
    """保留旧入口但不在启动后检查、下载或重启，资源由启动器预先应用。"""
    return


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
    await run_step(
        "模块",
        lambda: _call_existing_singleton(ModuleManager, "shutdown"),
        offload=True,
    )
    await run_step(
        "事件消费",
        lambda: _call_existing_singleton(EventManager, "stop_async"),
    )
    await run_step("浏览器会话", close_browser_sessions, offload=True)
    await run_step("托管资源", stop_managed_resources)
    await run_step(
        "DoH服务",
        stop_doh_composition,
        offload=True,
    )
    await run_step(
        "线程池",
        lambda: _call_existing_singleton(ThreadHelper, "shutdown"),
        offload=True,
    )
    await run_step(
        "Redis缓存连接",
        lambda: _call_existing_singleton(RedisHelper, "close"),
        offload=True,
    )
    await run_step(
        "异步Redis缓存连接",
        lambda: _call_existing_singleton(AsyncRedisHelper, "close"),
    )
    # Web Agent 的取消 finally 可能还要写入最终展示快照，必须先完成任务收尾，再关闭写入准入。
    web_agent_drained = await run_step(
        "Web Agent后台任务",
        shutdown_web_agent_background_tasks,
        record_failure=False,
    )
    if not web_agent_drained:
        web_agent_drained = await run_step("Web Agent后台任务收尾", wait_web_agent_background_tasks)
    if web_agent_drained:
        try:
            persistence = get_configured_agent_chat_persistence()
        except RuntimeError:
            persistence = None
        if persistence is None:
            persistence_drained = True
        else:
            await run_step(
                "Agent会话持久化准入",
                persistence.begin_shutdown,
            )
            persistence_drained = await run_step(
                "Agent会话持久化",
                persistence.shutdown,
            )
    else:
        persistence_drained = False
        all_converged = False
        logger.error("Web Agent任务未完成收尾，跳过持久化和数据库关闭以保护活动事务")
    if persistence_drained:
        await run_step("数据库任务", stop_database_runtime)
    await run_step("前端服务", stop_frontend, offload=True)
    await run_step("临时文件", clear_temp, offload=True)
    if persistence_drained:
        if not database_runtime_active():
            await run_step("模块 Provider", reset_module_providers)
            await run_step("数据库连接", close_database)
        else:
            all_converged = False
            logger.error(
                "数据库任务未收敛，保留全部 Provider 和数据库连接以供诊断与重试"
            )
    return all_converged


async def _initialize_modules() -> HostRuntime:
    """
    构造模块服务并返回本次 lifespan 唯一的类型化 HostRuntime。
    """
    if not ThreadHelper().reopen():
        raise RuntimeError("上一应用生命周期的共享线程池尚未收敛")
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
    classification = await compose_classification(
        executor=database_runtime.worker,
        settings=legacy_settings,
        system_config=configuration.system_config,
        extra_fields_provider=_plugin_classification_fields,
        extension_facts_provider=_plugin_classification_facts,
        enrichment=compose_classification_enrichment(
            module_manager=_existing_module_manager,
            plugin_manager=get_existing_plugin_manager,
        ),
    )
    database_services = compose_database_services(
        runtime=database_runtime,
        system_config=configuration.system_config,
    )
    system_config = configuration.system_config
    workflow_query = database_services.workflow_query
    runtime_dependencies = compose_runtime_dependencies()
    agent_composition = compose_agent(
        runtime=database_runtime,
        system_config=system_config,
        dependencies=runtime_dependencies,
    )
    security_composition = configure_security_services()
    runtime_composition = compose_runtime(
        RuntimeInputs(
            configuration=configuration,
            database=database_services,
            agent=agent_composition,
            authentication=security_composition,
            classification=classification.runtime,
            classification_execution=classification.execution,
            dependencies=runtime_dependencies,
            tasks=get_task_registry(),
        )
    )
    host_runtime = runtime_composition.runtime
    publish_database_services(database_services)
    publish_runtime(runtime_composition)
    configure_runtime_data_providers()
    configure_server_services(workflow_query, runtime_dependencies.subscription)
    configure_workflow_execution_composition()
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
    from app.agent.tools.manager import moviepilot_tool_manager

    moviepilot_tool_manager.set_data_context(agent_composition.data)
    configure_scheduler_agent_tasks(agent_composition.tasks)
    # 托管资源只在这里装配声明与 adapter，具体资源仍由首个消费者显式激活。
    init_managed_resources()
    # 应用服务不反向依赖 Chain，由启动组合层注入壁纸来源。
    configure_wallpaper_services()
    # 目录选择和多级路径生成复用同一活动分类策略快照。
    configure_directory_classification_service(classification.runtime)
    # Chain 无参兼容入口由组合根明确提供依赖上下文；测试和新代码可直接注入替代上下文。
    configure_chain_runtime_context(
        dependencies=runtime_dependencies,
        system_config=system_config,
        configuration=configuration.runtime.chain,
        classification_service=classification.execution,
    )
    # 认证访问层不反向依赖数据库实现，由启动组合层注入载荷提供器。
    configure_security_access()
    # DoH
    configure_doh_composition()
    # 站点管理
    sites_helper = SitesHelper()
    # 启动器已在进程拉起前应用待安装资源；这里仅注册当前版本读取器。
    configure_site_resource_versions(
        lambda: (sites_helper.auth_version, sites_helper.indexer_version)
    )
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
            if await stop_modules() is False:
                logger.error("模块启动失败后的资源 owner 未完全收敛")
        except Exception as cleanup_error:  # noqa: BLE001  保留原始启动异常
            logger.error(f"模块启动失败后的统一资源清理失败：{cleanup_error}")
        raise
