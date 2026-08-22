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

from app.application.orchestration.system import SystemChain
from app.application.plugin.runtime import get_plugin_manager
from app.runtime.config import global_vars, settings
from app.runtime.health import get_application_health
from app.runtime.topology import validate_process_topology
from app.adapters.external.server import MoviePilotServerHelper
from app.runtime.state import SystemHelper
from app.runtime.log import logger, LoggerManager
from app.startup.command_initializer import init_command, stop_command, restart_command
from app.startup.dataports_initializer import configure_data_ports
from app.startup.domain_initializer import configure_domain_dependencies
from app.startup.modules_initializer import init_modules, stop_modules
from app.startup.monitor_initializer import stop_monitor, init_monitor
from app.startup.plugins_initializer import (
    configure_plugin_services,
    execute_task,
    init_plugins,
    stop_plugins,
    sync_plugins,
)
from app.startup.routers_initializer import init_routers
from app.startup.scheduler_initializer import (
    stop_scheduler,
    init_scheduler,
    init_plugin_scheduler,
)
from app.db.engine import check_connection_budget, get_engine, get_global_async_engine
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
    plugin_manager = get_plugin_manager()
    try:
        if await sync_plugins():
            await execute_task(
                global_vars.loop,
                init_plugin_scheduler,
                "插件定时服务刷新",
            )
            await asyncio.wrap_future(restart_command())
    finally:
        plugin_manager.set_plugin_settling(False)
        plugin_manager.start_monitor()
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


async def initialize_modules_component(app: FastAPI) -> None:
    """启动模块并把其类型化运行时发布到当前 FastAPI AppState。"""
    runtime = await init_modules()
    if runtime is not None:
        app.state.host_runtime = runtime


def prepare_plugin_restore() -> None:
    """先装配插件外部系统服务，再恢复插件及其依赖。"""
    configure_plugin_services()
    SystemChain().restore_plugins()


def prepare_database_component(app: FastAPI) -> None:
    """完成数据库建表、迁移与 head 校验后发布数据库就绪状态。"""
    # Alembic 及全部 ORM 元数据只在 lifespan 真正启动时加载，create_app/import 阶段
    # 继续保持不建库、不加载迁移运行时的纯 ASGI 结构语义。
    from app.startup.database_initializer import (
        prepare_database,
        verify_database_revision,
    )

    prepare_database()
    verify_database_revision()
    get_application_health(app).mark_database_ready()


def build_lifecycle_components(app: FastAPI) -> tuple[LifecycleComponent, ...]:
    """按现有顺序构建应用组件清单，回调在每次 lifespan 启动时重新绑定。"""
    return (
        LifecycleComponent(
            name="数据库准备",
            start=lambda: prepare_database_component(app),
            start_order=10,
            start_timeout_seconds=300,
        ),
        LifecycleComponent(
            name="HTTP 基础能力",
            start=lambda: configure_default_user_agent(settings.USER_AGENT),
            stop=aclose_shared_async_transports,
            start_order=20,
            stop_order=80,
            start_timeout_seconds=30,
            stop_timeout_seconds=120,
        ),
        LifecycleComponent(
            name="领域依赖装配",
            dependencies=("HTTP 基础能力",),
            start=configure_domain_dependencies,
            start_order=30,
            start_timeout_seconds=30,
        ),
        LifecycleComponent(
            name="数据库引擎预热",
            dependencies=("数据库准备", "领域依赖装配"),
            start=lambda: (get_engine(), get_global_async_engine()),
            start_order=40,
            start_timeout_seconds=120,
        ),
        LifecycleComponent(
            name="数据库连接预算",
            dependencies=("数据库引擎预热",),
            start=check_connection_budget,
            start_order=50,
            start_timeout_seconds=30,
        ),
        LifecycleComponent(
            name="数据端口装配",
            dependencies=("数据库连接预算",),
            start=configure_data_ports,
            start_order=55,
            start_timeout_seconds=30,
        ),
        LifecycleComponent(
            name="路由",
            dependencies=("数据端口装配",),
            start=lambda: init_routers(app),
            start_order=60,
            start_timeout_seconds=30,
        ),
        LifecycleComponent(
            name="模块服务",
            dependencies=("路由",),
            start=lambda: initialize_modules_component(app),
            stop=stop_modules,
            start_order=70,
            stop_order=70,
            start_timeout_seconds=300,
            stop_timeout_seconds=300,
        ),
        LifecycleComponent(
            name="插件备份恢复",
            dependencies=("模块服务",),
            mode=LifecycleMode.NORMAL_ONLY,
            start=prepare_plugin_restore,
            start_order=80,
            start_timeout_seconds=300,
        ),
        LifecycleComponent(
            name="插件",
            dependencies=("插件备份恢复",),
            mode=LifecycleMode.NORMAL_ONLY,
            start=init_plugins,
            stop=stop_plugins,
            start_order=90,
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
            start_order=100,
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
            start_order=110,
            stop_order=40,
            start_timeout_seconds=120,
            stop_timeout_seconds=120,
        ),
        LifecycleComponent(
            name="待处理整理回放",
            dependencies=("监控器",),
            mode=LifecycleMode.NORMAL_ONLY,
            start=replay_pending_transfers,
            start_order=120,
            start_timeout_seconds=30,
        ),
        LifecycleComponent(
            name="命令服务",
            dependencies=("待处理整理回放",),
            mode=LifecycleMode.NORMAL_ONLY,
            start=init_command,
            stop=stop_command,
            start_order=130,
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
            start_order=140,
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
    health = get_application_health(app)
    health.begin_startup()
    try:
        validate_process_topology(
            workers=settings.API_WORKERS,
            safe_mode=settings.MOVIEPILOT_SAFE_MODE,
        )
        print("Starting up...")
        # 存储当前循环
        global_vars.set_loop(asyncio.get_event_loop())
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
        health.mark_ready()
    except BaseException:
        # Uvicorn 在 lifespan 抛错时不会开始接流量；状态仍需供嵌入式入口和测试诊断。
        health.mark_failed()
        raise
    try:
        # 在此处 yield，表示应用已经启动，控制权交回 FastAPI 主事件循环
        yield
    finally:
        health.mark_stopping()
        print("Shutting down...")
        global_vars.stop_system()
        # 插件恢复会在线程池中修改源码与依赖，必须完成后再进入资源关闭阶段。
        try:
            await sync_plugins_task
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
