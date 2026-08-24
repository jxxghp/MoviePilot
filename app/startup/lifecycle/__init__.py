"""应用生命周期组件的组装、启动和关闭编排。"""

import asyncio
import inspect
import time
from contextlib import asynccontextmanager
from typing import Awaitable, Callable

from fastapi import FastAPI

from app.startup.initializers.cache import configure_cache_dependencies
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
from app.application.plugin.lifecycle import plugin_lifecycle
from app.application.plugin.runtime import get_plugin_manager
from app.runtime.config import global_vars
from app.runtime.settings import RuntimeSettingsCompat
from app.foundation.environment import is_free_threaded_runtime, is_gil_enabled

settings = RuntimeSettingsCompat()
from app.runtime.health import get_application_health
from app.runtime.execution import run_in_threadpool_to_completion
from app.runtime.topology import validate_process_topology
from app.runtime.tasks import TaskRegistry, configure_task_registry
from app.adapters.external.server import MoviePilotServerHelper
from app.runtime.state import SystemHelper
from app.runtime.log import logger, LoggerManager
from app.startup.initializers.command import init_command, restart_command
from app.startup.initializers.agent import stop_agent
from app.startup.initializers.domain import configure_domain_dependencies
from app.startup.initializers.modules import (
    drain_events,
    init_modules,
    settle_events,
    stop_modules,
)
from app.startup.initializers.monitor import stop_monitor, init_monitor
from app.startup.initializers.plugins import (
    configure_plugin_services,
    execute_task,
    finalize_plugins,
    init_plugins,
    quiesce_plugin_services,
    quiesce_plugins,
    stop_plugin_monitor,
    sync_plugins,
)
from app.startup.initializers.routers import init_routers
from app.startup.initializers.scheduler import (
    stop_scheduler,
    init_scheduler,
    init_plugin_scheduler,
)
from app.db.engine import check_connection_budget, get_engine, get_global_async_engine
from app.startup.initializers.transfer import (
    replay_pending_transfers,
    stop_transfer_runtime,
)
from app.startup.initializers.workflow import init_workflow, stop_workflow
from app.startup.lifecycle.components import (
    LifecycleComponent,
    LifecycleFailurePolicy,
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
        _log_runtime_gil_status()
        return
    plugin_manager = get_plugin_manager()
    try:
        async with plugin_lifecycle.hold_startup():
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
        _log_runtime_gil_status()
    # 设置系统已修改标志
    SystemHelper().set_system_modified()
    # 重启完成
    SystemChain().restart_finish()
    # 上报当前安装版本
    await MoviePilotServerHelper.async_report_usage()


def _log_runtime_gil_status() -> None:
    """在核心模块和插件完成导入后记录解释器的实际并发模式。"""
    free_threaded = is_free_threaded_runtime()
    gil_enabled = is_gil_enabled()
    if free_threaded and gil_enabled:
        logger.warning(
            "Python free-threaded 运行时已启用 GIL，请检查此前的原生扩展兼容告警"
        )
        return
    logger.info(
        "Python运行时：%s，GIL=%s",
        "free-threaded" if free_threaded else "standard",
        "enabled" if gil_enabled else "disabled",
    )


async def run_shutdown_step(
    name: str,
    callback: Callable[[], object],
    timeout_seconds: float | None = None,
) -> bool:
    """在有限预算内执行关闭阶段，并返回资源 owner 是否已经收敛。"""

    async def invoke() -> object:
        """在主循环调用 owner，并等待其可能返回的异步结果。"""
        result = callback()
        if inspect.isawaitable(result):
            return await result
        return result

    try:
        task = asyncio.create_task(invoke(), name=f"shutdown.{name}")

        def _consume_shutdown_result(done: asyncio.Future) -> None:
            """消费延迟收敛任务的最终异常，避免事件循环产生未取回异常。"""
            try:
                done.result()
            except asyncio.CancelledError:
                pass
            except Exception as err:
                logger.error(f"关闭{name}最终收尾失败：{err}")

        task.add_done_callback(_consume_shutdown_result)
        if timeout_seconds:
            try:
                result = await asyncio.wait_for(
                    asyncio.shield(task), timeout=timeout_seconds
                )
            except asyncio.TimeoutError:
                logger.error("关闭%s超时，已请求取消并保留未收敛任务", name)
                task.cancel()
                return False
        else:
            result = await task
        if result is False:
            logger.error("关闭%s未收敛，资源所有权保持不变", name)
            return False
        return True
    except Exception as err:
        logger.error(f"关闭{name}失败：{err}")
        return False


def offload_shutdown_callback(
    callback: Callable[[], object],
) -> Callable[[], Awaitable[object]]:
    """把明确会阻塞的同步关闭 owner 包装为异步生命周期回调。"""

    async def invoke() -> object:
        return await run_in_threadpool_to_completion(callback)

    return invoke


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


async def stop_lifecycle_components(
    components: tuple[LifecycleComponent, ...],
) -> bool:
    """按声明顺序关闭组件，并在关键 owner 未收敛时停止释放依赖。"""
    all_converged = True
    for component in sorted(
        (item for item in components if item.stop is not None),
        key=lambda item: item.stop_order or 0,
    ):
        completed = await run_shutdown_step(
            component.name,
            component.stop,
            component.stop_timeout_seconds,
        )
        if completed:
            continue
        all_converged = False
        if component.stop_failure is LifecycleFailurePolicy.FAIL_FAST:
            logger.error(
                "关闭%s未收敛，停止释放其后续依赖",
                component.name,
            )
            break
    return all_converged


def select_startup_cleanup_components(
    components: tuple[LifecycleComponent, ...],
    *,
    started_names: set[str],
    active_component: LifecycleComponent | None,
) -> tuple[LifecycleComponent, ...]:
    """选择启动失败时已启动、部分启动及其 stop-only owner 的清理集合。"""
    cleanup_names = set(started_names)
    if active_component is not None:
        cleanup_names.add(active_component.name)

    # stop-only owner 没有启动回调，按已激活依赖递归纳入，避免清理时反向
    # 实例化尚未触达的插件、模块或外部资源。
    changed = True
    while changed:
        changed = False
        for component in components:
            if (
                component.stop is None
                or component.start is not None
                or component.name in cleanup_names
                or not set(component.dependencies).issubset(cleanup_names)
            ):
                continue
            cleanup_names.add(component.name)
            changed = True

    return tuple(
        component
        for component in components
        if component.stop is not None and component.name in cleanup_names
    )


async def initialize_modules_component(app: FastAPI) -> None:
    """启动模块并把其类型化运行时发布到当前 FastAPI AppState。"""
    try:
        runtime = await init_modules()
    except BaseException:
        from app.startup.initializers.modules import stop_database_worker

        try:
            await stop_database_worker()
        except Exception as cleanup_error:  # noqa: BLE001  保留原始启动异常
            logger.error(f"启动失败后的数据库任务清理失败：{cleanup_error}")
        raise
    if runtime is not None:
        app.state.host_runtime = runtime


def initialize_task_registry(app: FastAPI) -> None:
    """创建当前 lifespan 独占的后台任务登记器。"""
    task_registry = TaskRegistry()
    app.state.task_registry = task_registry
    configure_task_registry(task_registry)


async def stop_task_registry(app: FastAPI) -> bool:
    """停止接收新后台任务，并把已封口登记器保留到下一次显式启动。"""
    task_registry = getattr(app.state, "task_registry", None)
    if not isinstance(task_registry, TaskRegistry):
        configure_task_registry(None)
        app.state.task_registry = None
        return True
    # 即使 owner 已收敛，也不能在后续插件/模块 stop hook 仍会运行时退回永久
    # accepting 的兼容默认登记器。下一次 initialize_task_registry 会显式替换它。
    return await task_registry.shutdown(timeout_seconds=30.0)


def prepare_plugin_restore() -> None:
    """先装配插件外部系统服务，再恢复插件及其依赖。"""
    configure_plugin_services()
    SystemChain().restore_plugins()


def prepare_database_component(app: FastAPI) -> None:
    """完成数据库建表、迁移与 head 校验后发布数据库就绪状态。"""
    # Alembic 及全部 ORM 元数据只在 lifespan 真正启动时加载，create_app/import 阶段
    # 继续保持不建库、不加载迁移运行时的纯 ASGI 结构语义。
    from app.startup.initializers.database import (
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
            name="后台任务登记器",
            start=lambda: initialize_task_registry(app),
            stop=lambda: stop_task_registry(app),
            start_order=5,
            stop_order=5,
            start_timeout_seconds=30,
            stop_timeout_seconds=60,
            stop_failure=LifecycleFailurePolicy.FAIL_FAST,
        ),
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
            name="路由",
            dependencies=("数据库连接预算",),
            start=lambda: init_routers(app, settings.API_V1_STR),
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
            stop=offload_shutdown_callback(finalize_plugins),
            start_order=90,
            stop_order=60,
            start_timeout_seconds=300,
            stop_timeout_seconds=300,
            stop_failure=LifecycleFailurePolicy.FAIL_FAST,
        ),
        LifecycleComponent(
            name="插件变更监控",
            dependencies=("插件",),
            mode=LifecycleMode.NORMAL_ONLY,
            stop=offload_shutdown_callback(stop_plugin_monitor),
            stop_order=8,
            stop_timeout_seconds=10,
            stop_failure=LifecycleFailurePolicy.FAIL_FAST,
        ),
        LifecycleComponent(
            name="定时器",
            dependencies=("插件",),
            mode=LifecycleMode.NORMAL_ONLY,
            start=init_scheduler,
            stop=offload_shutdown_callback(stop_scheduler),
            start_order=100,
            stop_order=50,
            start_timeout_seconds=120,
            stop_timeout_seconds=120,
            stop_failure=LifecycleFailurePolicy.FAIL_FAST,
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
            stop_failure=LifecycleFailurePolicy.FAIL_FAST,
        ),
        LifecycleComponent(
            name="整理后台服务",
            dependencies=("模块服务",),
            stop=stop_transfer_runtime,
            stop_order=52,
            stop_timeout_seconds=45,
            stop_failure=LifecycleFailurePolicy.FAIL_FAST,
        ),
        LifecycleComponent(
            name="AI智能体会话",
            dependencies=("模块服务",),
            stop=stop_agent,
            stop_order=51,
            stop_timeout_seconds=300,
            stop_failure=LifecycleFailurePolicy.FAIL_FAST,
        ),
        LifecycleComponent(
            name="插件事件入口",
            dependencies=("插件",),
            mode=LifecycleMode.NORMAL_ONLY,
            stop=quiesce_plugins,
            stop_order=53,
            stop_timeout_seconds=300,
            stop_failure=LifecycleFailurePolicy.FAIL_FAST,
        ),
        LifecycleComponent(
            name="事件尾任务结算",
            dependencies=("模块服务", "插件"),
            mode=LifecycleMode.NORMAL_ONLY,
            stop=settle_events,
            stop_order=54,
            stop_timeout_seconds=120,
            stop_failure=LifecycleFailurePolicy.FAIL_FAST,
        ),
        LifecycleComponent(
            name="插件后台服务",
            dependencies=("插件",),
            mode=LifecycleMode.NORMAL_ONLY,
            stop=quiesce_plugin_services,
            stop_order=55,
            stop_timeout_seconds=300,
            stop_failure=LifecycleFailurePolicy.FAIL_FAST,
        ),
        LifecycleComponent(
            name="事件投递屏障",
            dependencies=("模块服务", "整理后台服务"),
            stop=drain_events,
            stop_order=58,
            stop_timeout_seconds=120,
            stop_failure=LifecycleFailurePolicy.FAIL_FAST,
        ),
        LifecycleComponent(
            name="待处理整理回放",
            dependencies=("监控器", "整理后台服务"),
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
            start_order=130,
            start_timeout_seconds=120,
        ),
        LifecycleComponent(
            name="工作流",
            dependencies=("命令服务",),
            mode=LifecycleMode.NORMAL_ONLY,
            start=init_workflow,
            stop=offload_shutdown_callback(stop_workflow),
            start_order=140,
            stop_order=20,
            start_timeout_seconds=120,
            stop_timeout_seconds=120,
            stop_failure=LifecycleFailurePolicy.FAIL_FAST,
        ),
        LifecycleComponent(
            name="插件备份",
            dependencies=("插件",),
            mode=LifecycleMode.NORMAL_ONLY,
            stop=offload_shutdown_callback(
                lambda: SystemChain().backup_plugins()
            ),
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
    main_loop = asyncio.get_running_loop()
    main_loop_owner: object | None = None
    enabled_components: tuple[LifecycleComponent, ...] = ()
    started_component_names: set[str] = set()
    active_start_component: LifecycleComponent | None = None
    try:
        validate_process_topology(
            workers=settings.API_WORKERS,
            safe_mode=settings.MOVIEPILOT_SAFE_MODE,
        )
        print("Starting up...")
        main_loop_owner = global_vars.set_loop(main_loop)
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
            active_start_component = component
            await run_startup_step(
                component.name,
                component.start,
                component.start_timeout_seconds,
            )
            started_component_names.add(component.name)
            active_start_component = None
        if settings.MOVIEPILOT_SAFE_MODE:
            print("MoviePilot safe mode enabled: skip plugins, scheduler, monitor, commands and workflow.")
        # 插件同步到本地
        sync_plugins_task = asyncio.create_task(
            run_startup_step("插件同步与启动收尾", init_extra)
        )
        task_registry = app.state.task_registry
        task_registry.register(sync_plugins_task, owner="startup.plugin_settlement")
        health.mark_ready()
    except BaseException:
        # Uvicorn 在 lifespan 抛错时不会开始接流量；状态仍需供嵌入式入口和测试诊断。
        health.mark_failed()
        cleanup_components = select_startup_cleanup_components(
            enabled_components,
            started_names=started_component_names,
            active_component=active_start_component,
        )
        try:
            await stop_lifecycle_components(cleanup_components)
        except Exception as cleanup_error:
            logger.error(f"启动失败后的生命周期清理失败：{cleanup_error}")
        finally:
            if main_loop_owner is not None:
                global_vars.clear_loop(main_loop_owner)
        raise
    try:
        # 在此处 yield，表示应用已经启动，控制权交回 FastAPI 主事件循环
        yield
    finally:
        health.mark_stopping()
        print("Shutting down...")
        global_vars.stop_system()
        try:
            # 插件 settlement 已登记到最前置 TaskRegistry。由该 FAIL_FAST owner
            # 在统一预算内取消/等待，不能在屏障之前无界 await 绕过停机预算。
            await stop_lifecycle_components(enabled_components)
        finally:
            try:
                # 日志最后关闭，确保其他组件的收尾信息已写入文件
                if LoggerManager.shutdown() is False:
                    raise RuntimeError("日志写入资源未在关停预算内收敛")
            finally:
                if main_loop_owner is not None:
                    global_vars.clear_loop(main_loop_owner)
