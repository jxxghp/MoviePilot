"""应用生命周期组件的组装、启动和关闭编排。"""

import asyncio
import inspect
import time
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
from app.startup.lifecycle.components import (
    LifecycleComponent,
    LifecycleMode,
    lifecycle_manifest,
)
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


async def run_shutdown_step(
    name: str,
    callback: Callable[[], object],
    timeout_seconds: float | None = None,
) -> None:
    """隔离单个关闭阶段的异常，确保后续资源仍有机会释放"""
    try:
        result = callback()
        if inspect.isawaitable(result):
            if timeout_seconds:
                await asyncio.wait_for(result, timeout=timeout_seconds)
            else:
                await result
    except Exception as err:
        logger.error(f"关闭{name}失败：{err}")


async def run_startup_step(
    name: str,
    callback: Callable[[], object],
    timeout_seconds: float | None = None,
) -> object:
    """执行单个启动阶段并记录耗时，失败时保留原异常和 fail-fast 语义。"""
    started_at = time.perf_counter()
    try:
        result = callback()
        if inspect.isawaitable(result):
            if timeout_seconds:
                result = await asyncio.wait_for(result, timeout=timeout_seconds)
            else:
                result = await result
        return result
    finally:
        elapsed_ms = (time.perf_counter() - started_at) * 1000
        logger.info("启动%s完成，耗时=%.2fms", name, elapsed_ms)


def build_lifecycle_components(app: FastAPI) -> tuple[LifecycleComponent, ...]:
    """按现有顺序构建应用组件清单，回调在每次 lifespan 启动时重新绑定。"""
    return (
        LifecycleComponent(
            name="HTTP 基础能力",
            start=lambda: configure_default_user_agent(settings.USER_AGENT),
            stop=aclose_shared_async_transports,
            start_order=10,
            stop_order=80,
            start_timeout_seconds=30,
            stop_timeout_seconds=120,
        ),
        LifecycleComponent(
            name="领域依赖装配",
            dependencies=("HTTP 基础能力",),
            start=configure_domain_dependencies,
            start_order=20,
            start_timeout_seconds=30,
        ),
        LifecycleComponent(
            name="数据库引擎预热",
            dependencies=("领域依赖装配",),
            start=lambda: (get_engine(), get_global_async_engine()),
            start_order=30,
            start_timeout_seconds=120,
        ),
        LifecycleComponent(
            name="数据库连接预算",
            dependencies=("数据库引擎预热",),
            start=check_connection_budget,
            start_order=40,
            start_timeout_seconds=30,
        ),
        LifecycleComponent(
            name="路由",
            dependencies=("数据库连接预算",),
            start=lambda: init_routers(app),
            start_order=50,
            start_timeout_seconds=30,
        ),
        LifecycleComponent(
            name="模块服务",
            dependencies=("路由",),
            start=init_modules,
            stop=stop_modules,
            start_order=60,
            stop_order=70,
            start_timeout_seconds=300,
            stop_timeout_seconds=300,
        ),
        LifecycleComponent(
            name="插件备份恢复",
            dependencies=("模块服务",),
            mode=LifecycleMode.NORMAL_ONLY,
            start=lambda: SystemChain().restore_plugins(),
            start_order=70,
            start_timeout_seconds=300,
        ),
        LifecycleComponent(
            name="插件",
            dependencies=("插件备份恢复",),
            mode=LifecycleMode.NORMAL_ONLY,
            start=init_plugins,
            stop=stop_plugins,
            start_order=80,
            stop_order=60,
            start_timeout_seconds=300,
            stop_timeout_seconds=300,
        ),
        LifecycleComponent(
            name="定时器",
            dependencies=("插件",),
            mode=LifecycleMode.NORMAL_ONLY,
            start=init_scheduler,
            stop=stop_scheduler,
            start_order=90,
            stop_order=50,
            start_timeout_seconds=120,
            stop_timeout_seconds=120,
        ),
        LifecycleComponent(
            name="监控器",
            dependencies=("定时器",),
            mode=LifecycleMode.NORMAL_ONLY,
            start=init_monitor,
            stop=stop_monitor,
            start_order=100,
            stop_order=40,
            start_timeout_seconds=120,
            stop_timeout_seconds=120,
        ),
        LifecycleComponent(
            name="待处理整理回放",
            dependencies=("监控器",),
            mode=LifecycleMode.NORMAL_ONLY,
            start=replay_pending_transfers,
            start_order=110,
            start_timeout_seconds=30,
        ),
        LifecycleComponent(
            name="命令服务",
            dependencies=("待处理整理回放",),
            mode=LifecycleMode.NORMAL_ONLY,
            start=init_command,
            stop=stop_command,
            start_order=120,
            stop_order=30,
            start_timeout_seconds=120,
            stop_timeout_seconds=120,
        ),
        LifecycleComponent(
            name="工作流",
            dependencies=("命令服务",),
            mode=LifecycleMode.NORMAL_ONLY,
            start=init_workflow,
            stop=stop_workflow,
            start_order=130,
            stop_order=20,
            start_timeout_seconds=120,
            stop_timeout_seconds=120,
        ),
        LifecycleComponent(
            name="插件备份",
            dependencies=("插件",),
            mode=LifecycleMode.NORMAL_ONLY,
            stop=lambda: SystemChain().backup_plugins(),
            stop_order=10,
            stop_timeout_seconds=300,
        ),
    )


def get_lifecycle_manifest(app: FastAPI, *, safe_mode: bool) -> tuple[dict[str, object], ...]:
    """导出指定模式下的生命周期组件、依赖、顺序和超时清单。"""
    return lifecycle_manifest(
        build_lifecycle_components(app),
        safe_mode=safe_mode,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    定义应用的生命周期事件
    """
    print("Starting up...")
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
    components = build_lifecycle_components(app)
    enabled_components = tuple(
        component
        for component in components
        if component.enabled(settings.MOVIEPILOT_SAFE_MODE)
    )
    logger.info(
        "启用生命周期组件：%s",
        ", ".join(component.name for component in enabled_components),
    )
    for component in sorted(
        (item for item in enabled_components if item.start is not None),
        key=lambda item: item.start_order or 0,
    ):
        await run_startup_step(
            component.name,
            component.start,
            component.start_timeout_seconds,
        )
    if settings.MOVIEPILOT_SAFE_MODE:
        print("MoviePilot safe mode enabled: skip plugins, scheduler, monitor, commands and workflow.")
    # 插件同步到本地
    sync_plugins_task = asyncio.create_task(
        run_startup_step("插件同步与启动收尾", init_extra)
    )
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
            for component in sorted(
                (item for item in enabled_components if item.stop is not None),
                key=lambda item: item.stop_order or 0,
            ):
                await run_shutdown_step(
                    component.name,
                    component.stop,
                    component.stop_timeout_seconds,
                )
        finally:
            # 日志最后关闭，确保其他组件的收尾信息已写入文件
            LoggerManager.shutdown()
