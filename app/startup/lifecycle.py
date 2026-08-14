import asyncio
import inspect
from contextlib import asynccontextmanager
from typing import Callable

from fastapi import FastAPI

from app.startup.cache_initializer import configure_cache_dependencies
# 缓存装饰器会在业务模块导入时创建后端，必须先完成适配器装配。
configure_cache_dependencies()
# urllib3-future 覆盖 urllib3 命名空间后删除了 format_header_param，导致 telebot 崩溃，需在加载模块前打补丁
try:
    import urllib3.fields as _urllib3_fields

    if not hasattr(_urllib3_fields, "format_header_param") and hasattr(
        _urllib3_fields, "format_header_param_rfc2231"
    ):
        _urllib3_fields.format_header_param = (
            _urllib3_fields.format_header_param_rfc2231
        )
except Exception:
    pass

from app.chain.system import SystemChain
from app.runtime.config import global_vars, settings
from app.adapters.external.server import MoviePilotServerHelper
from app.runtime.state import SystemHelper
from app.runtime.log import logger, LoggerManager
from app.startup.command_initializer import init_command, stop_command, restart_command
from app.startup.domain_initializer import configure_domain_dependencies
from app.startup.modules_initializer import init_modules, stop_modules
from app.startup.monitor_initializer import stop_monitor, init_monitor
from app.startup.plugins_initializer import init_plugins, stop_plugins, sync_plugins
from app.startup.routers_initializer import init_routers
from app.startup.scheduler_initializer import (
    stop_scheduler,
    init_scheduler,
    init_plugin_scheduler,
)
from app.db import check_connection_budget, get_engine, get_global_async_engine
from app.startup.transfer_initializer import replay_pending_transfers
from app.startup.workflow_initializer import init_workflow, stop_workflow
from app.adapters.network.http import (
    aclose_shared_async_transports,
    configure_default_user_agent,
)


async def init_extra():
    """
    同步插件及重启相关依赖服务
    """
    if settings.MOVIEPILOT_SAFE_MODE:
        SystemHelper().set_system_modified()
        SystemChain().restart_finish()
        return
    if await sync_plugins():
        # 重新注册插件定时服务
        init_plugin_scheduler()
        # 重新注册命令
        restart_command()
    # 设置系统已修改标志
    SystemHelper().set_system_modified()
    # 重启完成
    SystemChain().restart_finish()
    # 上报当前安装版本
    await MoviePilotServerHelper.async_report_usage()


async def run_shutdown_step(name: str, callback: Callable[[], object]) -> None:
    """隔离单个关闭阶段的异常，确保后续资源仍有机会释放"""
    try:
        result = callback()
        if inspect.isawaitable(result):
            await result
    except Exception as err:
        logger.error(f"关闭{name}失败：{err}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    定义应用的生命周期事件
    """
    print("Starting up...")
    # HTTP 基础能力不反向读取平台配置，由启动层注入宿主标识。
    configure_default_user_agent(settings.USER_AGENT)
    # 领域层只消费显式注入的配置和适配器，不自行读取平台或数据库。
    configure_domain_dependencies()
    # 存储当前循环
    global_vars.set_loop(asyncio.get_event_loop())
    # 同步与异步引擎各预热一次。引擎改为惰性创建后，两者的首次创建时机都不再由启动路径
    # 决定，这一步把它们拉回来。必须排在所有 init_* 之前，两个理由：
    #
    # 其一，fail-fast 的落点。异步驱动缺失、异步 URL 拼错这类问题若不在这里暴露，会一路
    # 推迟到第一个异步查询——表现为用户请求 500 或调度任务静默失败，而不是启动即崩。
    # 故意不 try/except：起不来就该起不来，吞掉它等于把 fail-fast 又还回去了。而既然会抛，
    # 就必须抛在 init_routers / init_modules 之前——下面的 try/finally 关停块要到 yield 处
    # 才开始，在它之后抛异常，已经初始化好的模块就拿不到 stop_modules() 了。
    #
    # 其二，同步引擎的首次创建要落在单线程期。init_db() 会顺带预热它，但那只对
    # run_application() 入口成立；外部 supervisor 直挂 ASGI app（如
    # `gunicorn -k uvicorn.workers.UvicornWorker app.factory:app`）时 init_db() 根本不执行，
    # 首次创建便退到运行期——而那时 init_scheduler() / init_monitor() 已经放出上百个线程，
    # 引擎构建里那段 PRAGMA journal_mode 会让它们一起堵在创建锁上。
    #
    # 代价：异步侧几乎为零，create_async_engine 只校验 URL 与驱动导入、不建立连接；同步侧
    # 会连一次库、设一遍 journal mode，在事件循环上阻塞一小会儿——但那一次本来就免不了，
    # 放在这里至少还独占着单线程，而且此刻 uvicorn 尚未开始接请求。
    get_engine()
    get_global_async_engine()
    # 核算数据库连接理论峰值。各连接池是彼此独立配置的，没有任何地方核算总和，
    # 超额只会在突发并发时以 TooManyConnectionsError 的形式暴露；这里在启动期
    # 就对照数据库的真实上限校验一次，把问题前移到可见的位置
    check_connection_budget()
    # 初始化路由
    init_routers(app)
    # 初始化模块
    await init_modules()
    if settings.MOVIEPILOT_SAFE_MODE:
        print("MoviePilot safe mode enabled: skip plugins, scheduler, monitor, commands and workflow.")
    else:
        # 恢复插件备份
        SystemChain().restore_plugins()
        # 初始化插件
        init_plugins()
        # 初始化定时器
        init_scheduler()
        # 初始化监控器
        init_monitor()
        # 回放上次未整理完的文件（后台线程，不阻塞启动）
        replay_pending_transfers()
        # 初始化命令
        init_command()
        # 初始化工作流
        init_workflow()
    # 插件同步到本地
    sync_plugins_task = asyncio.create_task(init_extra())
    try:
        # 在此处 yield，表示应用已经启动，控制权交回 FastAPI 主事件循环
        yield
    finally:
        print("Shutting down...")
        global_vars.stop_system()
        # 取消同步插件任务
        try:
            sync_plugins_task.cancel()
            await sync_plugins_task
        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(str(e))
        try:
            if not settings.MOVIEPILOT_SAFE_MODE:
                await run_shutdown_step(
                    "插件备份", lambda: SystemChain().backup_plugins()
                )
                await run_shutdown_step("工作流", stop_workflow)
                await run_shutdown_step("命令服务", stop_command)
                await run_shutdown_step("监控器", stop_monitor)
                await run_shutdown_step("定时器", stop_scheduler)
                await run_shutdown_step("插件", stop_plugins)
            await run_shutdown_step("模块服务", stop_modules)
            await run_shutdown_step(
                "共享异步 HTTP 连接池",
                aclose_shared_async_transports,
            )
        finally:
            # 日志最后关闭，确保其他组件的收尾信息已写入文件
            LoggerManager.shutdown()
