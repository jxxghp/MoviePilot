import inspect
import sys
from typing import Callable

from app.adapters.cache.redis import RedisHelper, AsyncRedisHelper
from app.chain.mediaserver import MediaServerChain
from app.chain.tmdb import TmdbChain

# SitesHelper涉及资源包拉取，提前引入并容错提示
try:
    from app.application.site.sites import SitesHelper  # noqa
except ImportError as e:
    SitesHelper = None
    error_message = f"错误: {str(e)}\n站点认证及索引相关资源导入失败，请尝试重建容器或手动拉取资源"
    print(error_message, file=sys.stderr)
    sys.exit(1)

from app.adapters.system.host import SystemUtils
from app.runtime.log import logger
from app.runtime.config import settings
from app.runtime.extensions.module_manager import ModuleManager
from app.runtime.events import EventHandlerBinding, EventManager
from app.runtime.state import SystemHelper
from app.runtime.thread import ThreadHelper
from app.adapters.network.doh import DohHelper
from app.adapters.system.resource import (
    ResourceHelper,
    configure_resource_version_provider,
)
from app.application.messaging.message import MessageHelper, stop_message
from app.adapters.external.server import (
    MoviePilotServerHelper,
    configure_server_application_services,
)
from app.application.server.report import ServerReportService
from app.application.server.share import ServerSharingService
from app.db import close_database
from app.db.oper.subscribe import SubscribeOper
from app.db.oper.systemconfig import SystemConfigOper
from app.db.oper.workflow import WorkflowOper
from app.command import CommandChain
from app.schemas.message import Message
from app.schemas.message import MessageType
from app.schemas.types import SystemConfigKey
from app.startup.agent_initializer import init_agent, stop_agent
from app.startup.managed_resources_initializer import (
    init_managed_resources,
    stop_managed_resources,
)
from app.application.security.access import set_superuser_token_payload_provider
from app.application.security.auth import build_superuser_token_payload
from app.application.image import configure_wallpaper_providers
from app.application.chain.context import (
    build_default_chain_runtime_context,
    configure_chain_runtime_context_provider,
)
from app.runtime.extensions.service_config import configure_service_config_reader


async def _async_get_subscribe(subscribe_id: int):
    """通过数据库操作器异步读取订阅，供服务端共享用例使用。"""
    return await SubscribeOper().async_get(subscribe_id)


async def _async_get_workflow(workflow_id: int):
    """通过数据库操作器异步读取工作流，供服务端共享用例使用。"""
    return await WorkflowOper().async_get(workflow_id)


def configure_runtime_data_providers() -> None:
    """在启动组合层装配运行时和外部服务所需的数据库读取能力。"""
    configure_service_config_reader(lambda key: SystemConfigOper().get(key))
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
    from app.chain.download import DownloadChain
    from app.chain.scraping import ScrapingChain
    from app.chain.search import SearchChain
    from app.chain.site import SiteChain
    from app.chain.subscribe import SubscribeChain
    from app.chain.workflow import WorkflowChain
    from app.command import Command
    from app.scheduler import Scheduler

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


async def init_modules():
    """
    启动模块
    """
    # 数据访问能力统一在启动组合根注入，Runtime 和 Adapter 不再直接依赖 Oper。
    configure_runtime_data_providers()
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
    # 启动事件消费
    EventManager().start()
    # 初始化共享服务端状态
    MoviePilotServerHelper.init_plugin_report()
    MoviePilotServerHelper.init_subscribe_report()
    MoviePilotServerHelper.get_user_uuid()
    MoviePilotServerHelper.get_github_user()
    # 初始化AI智能体
    await init_agent()
    # 启动前端服务
    start_frontend()
    # 检查认证状态
    check_auth()
