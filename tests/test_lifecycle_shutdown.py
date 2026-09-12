import asyncio
import signal
import threading
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI

from app.adapters.network import http as http_utils
from app.application.configuration import (
    get_configured_system_config,
    get_runtime_settings,
)
from app.runtime.config import settings as runtime_settings
from app.runtime.tasks import configure_task_registry, get_task_registry
from app.startup import lifecycle
from app.startup.composition.system import compose_system_service
from app.startup.initializers import modules as modules_initializer


@pytest.fixture(autouse=True)
def _isolate_task_registry():
    """生命周期用例结束后恢复未启动宿主时的默认任务登记器。"""
    configure_task_registry(None)
    yield
    configure_task_registry(None)


def _assert_completed_once(mock: MagicMock) -> None:
    if isinstance(mock, AsyncMock):
        mock.assert_awaited_once()
    else:
        mock.assert_called_once()


def _system_runtime():
    """构造使用真实系统控制适配器的最小运行时。"""

    @asynccontextmanager
    async def rule_group_mutation():
        """提供本组测试不会进入的规则组事务替身。"""
        yield SimpleNamespace()

    return SimpleNamespace(
        system=compose_system_service(
            settings=get_runtime_settings(),
            system_config=get_configured_system_config(),
            rule_group_mutation=rule_group_mutation,
        )
    )


def _patch_lifespan(monkeypatch, *, failing_step: str | None = None) -> dict:
    """隔离 lifespan 的外部依赖，并按名称注入一个关闭失败"""
    monkeypatch.setattr(runtime_settings, "MOVIEPILOT_SAFE_MODE", False)
    monkeypatch.setattr(lifecycle.main_loop_registry, "register", MagicMock())
    monkeypatch.setattr(lifecycle.main_loop_registry, "release", MagicMock())
    monkeypatch.setattr(lifecycle.runtime_stop_state, "stop_system", MagicMock())

    for name in (
        "init_routers",
        "init_plugins",
        "init_scheduler",
        "init_monitor",
        "replay_pending_transfers",
        "init_command",
        "init_workflow",
    ):
        monkeypatch.setattr(lifecycle, name, MagicMock())
    monkeypatch.setattr(lifecycle, "configure_plugin_runtime_services", MagicMock())
    monkeypatch.setattr(lifecycle, "configure_plugin_services", MagicMock())
    plugin_recovery = MagicMock()
    plugin_recovery.replay = AsyncMock()
    monkeypatch.setattr(
        lifecycle,
        "get_plugin_installation_recovery",
        MagicMock(return_value=plugin_recovery),
    )
    monkeypatch.setattr(lifecycle, "init_modules", AsyncMock())
    log_owner = object()

    def initialize_log(app: FastAPI) -> None:
        """发布隔离的日志 owner，覆盖正常启动和失败清理路径。"""
        app.state.log_writer = log_owner
        app.state.log_shutdown_failed = False

    logger_shutdown = MagicMock(return_value=True)

    def stop_log(app: FastAPI) -> bool:
        """按 mock 返回值模拟 owner 收敛并同步生命周期状态。"""
        converged = logger_shutdown.return_value is not False
        app.state.log_shutdown_failed = not converged
        if converged:
            app.state.log_writer = None
        return converged

    logger_shutdown.side_effect = stop_log
    message_shutdown = MagicMock(return_value=True)

    def stop_message(app: FastAPI) -> bool:
        """按 mock 返回值模拟消息 owner 的明确关闭结果。"""
        converged = message_shutdown.return_value is not False
        app.state.message_shutdown_failed = not converged
        return converged

    message_shutdown.side_effect = stop_message
    monkeypatch.setattr(
        lifecycle,
        "initialize_log_runtime",
        MagicMock(side_effect=initialize_log),
    )
    monkeypatch.setattr(lifecycle, "stop_log_runtime", logger_shutdown)
    monkeypatch.setattr(lifecycle, "initialize_message_runtime", MagicMock())
    monkeypatch.setattr(lifecycle, "stop_message_runtime", message_shutdown)

    # 启动期的引擎预热与额度核算也要打桩。不打的话这些用例会走真实的引擎创建，在测试
    # 进程里留下一个从此无人释放的全局异步引擎——NullPool 不持连接、无害，但用例就不再
    # 自洽了，而且额度核算还会去连库。
    for name in ("get_engine", "get_global_async_engine", "check_connection_budget"):
        monkeypatch.setattr(lifecycle, name, MagicMock())
    database_prepare = MagicMock(
        side_effect=lambda app: lifecycle.get_application_health(
            app
        ).mark_database_ready()
    )
    monkeypatch.setattr(
        lifecycle,
        "prepare_database_component",
        database_prepare,
    )

    system_chain = MagicMock()
    monkeypatch.setattr(lifecycle, "SystemChain", MagicMock(return_value=system_chain))
    monkeypatch.setattr(lifecycle, "init_extra", AsyncMock())

    shutdown_steps = {
        "backup_plugins": system_chain.backup_plugins,
        "stop_plugin_monitor": MagicMock(return_value=True),
        "stop_workflow": MagicMock(),
        "stop_monitor": MagicMock(),
        "stop_scheduler": MagicMock(),
        "stop_agent": AsyncMock(return_value=True),
        "stop_transfer": AsyncMock(return_value=True),
        "quiesce_plugins": AsyncMock(return_value=True),
        "settle_events": AsyncMock(return_value=True),
        "quiesce_plugin_services": AsyncMock(return_value=True),
        "drain_events": AsyncMock(return_value=True),
        "finalize_plugins": MagicMock(return_value=True),
        "stop_modules": AsyncMock(),
        "close_http": AsyncMock(),
        "message": message_shutdown,
        "logger": logger_shutdown,
    }
    for name in (
        "stop_workflow",
        "stop_monitor",
        "stop_scheduler",
        "stop_plugin_monitor",
        "finalize_plugins",
    ):
        monkeypatch.setattr(lifecycle, name, shutdown_steps[name])
    monkeypatch.setattr(lifecycle, "stop_agent", shutdown_steps["stop_agent"])
    monkeypatch.setattr(
        lifecycle,
        "stop_transfer_runtime",
        shutdown_steps["stop_transfer"],
    )
    monkeypatch.setattr(
        lifecycle,
        "quiesce_plugins",
        shutdown_steps["quiesce_plugins"],
    )
    monkeypatch.setattr(
        lifecycle,
        "settle_events",
        shutdown_steps["settle_events"],
    )
    monkeypatch.setattr(
        lifecycle,
        "quiesce_plugin_services",
        shutdown_steps["quiesce_plugin_services"],
    )
    monkeypatch.setattr(lifecycle, "drain_events", shutdown_steps["drain_events"])
    monkeypatch.setattr(lifecycle, "stop_modules", shutdown_steps["stop_modules"])
    monkeypatch.setattr(
        lifecycle,
        "aclose_shared_async_transports",
        shutdown_steps["close_http"],
    )

    if failing_step:
        shutdown_steps[failing_step].side_effect = RuntimeError(
            f"{failing_step} failed"
        )

    return shutdown_steps


@pytest.mark.parametrize(
    "failing_step",
    [
        "backup_plugins",
        "stop_modules",
        "close_http",
    ],
)
def test_lifespan_continues_after_each_shutdown_owner_failure(
    monkeypatch,
    failing_step,
):
    """任一关闭阶段失败都不能跳过后续资源所有者"""
    shutdown_steps = _patch_lifespan(monkeypatch, failing_step=failing_step)

    async def run_lifespan():
        async with lifecycle.lifespan(FastAPI()):
            pass

    asyncio.run(run_lifespan())

    lifecycle.runtime_stop_state.stop_system.assert_called_once_with()
    lifecycle.init_modules.assert_awaited_once_with()
    for step in shutdown_steps.values():
        _assert_completed_once(step)


def test_lifespan_normal_mode_starts_full_runtime(monkeypatch):
    """正常模式必须初始化插件及后台服务，并在退出时逐项停止。"""
    shutdown_steps = _patch_lifespan(monkeypatch)

    async def run_lifespan():
        async with lifecycle.lifespan(FastAPI()):
            pass

    asyncio.run(run_lifespan())

    lifecycle.main_loop_registry.release.assert_called_once_with(
        lifecycle.main_loop_registry.register.return_value
    )
    lifecycle.init_modules.assert_awaited_once_with()
    lifecycle.prepare_database_component.assert_called_once()
    lifecycle.configure_plugin_services.assert_called_once_with()
    for name in (
        "init_plugins",
        "init_scheduler",
        "init_monitor",
        "replay_pending_transfers",
        "init_command",
        "init_workflow",
    ):
        getattr(lifecycle, name).assert_called_once_with()
    for step in shutdown_steps.values():
        _assert_completed_once(step)


def test_lifespan_can_start_and_stop_resource_owners_twice(monkeypatch) -> None:
    """同一应用连续两轮 lifespan 必须各自启动并关闭日志和消息 owner。"""
    shutdown_steps = _patch_lifespan(monkeypatch)
    app = FastAPI()

    async def run_two_cycles() -> None:
        """连续运行两轮隔离生命周期。"""
        async with lifecycle.lifespan(app):
            pass
        async with lifecycle.lifespan(app):
            pass

    asyncio.run(run_two_cycles())

    assert lifecycle.initialize_log_runtime.call_count == 2
    assert lifecycle.initialize_message_runtime.call_count == 2
    assert shutdown_steps["message"].call_count == 2
    assert shutdown_steps["logger"].call_count == 2


def test_lifespan_propagates_logger_nonconvergence(monkeypatch):
    """最后一个日志 owner 未收敛时 lifespan 必须以关闭失败结束。"""
    shutdown_steps = _patch_lifespan(monkeypatch)
    shutdown_steps["logger"].return_value = False

    async def run_lifespan() -> None:
        """运行完整生命周期并触发日志 writer 的诚实失败结果。"""
        async with lifecycle.lifespan(FastAPI()):
            pass

    with pytest.raises(RuntimeError, match="日志写入资源未在关停预算内收敛"):
        asyncio.run(run_lifespan())

    for step in shutdown_steps.values():
        _assert_completed_once(step)
    lifecycle.main_loop_registry.release.assert_called_once_with(
        lifecycle.main_loop_registry.register.return_value
    )


def test_lifespan_propagates_message_nonconvergence(monkeypatch):
    """消息 owner 未收敛时 lifespan 必须保留资源并明确失败。"""
    shutdown_steps = _patch_lifespan(monkeypatch)
    shutdown_steps["message"].return_value = False

    async def run_lifespan() -> None:
        """运行完整生命周期并触发消息资源失败。"""
        async with lifecycle.lifespan(FastAPI()):
            pass

    with pytest.raises(RuntimeError, match="消息资源未在关停预算内收敛"):
        asyncio.run(run_lifespan())

    _assert_completed_once(shutdown_steps["message"])
    _assert_completed_once(shutdown_steps["logger"])


def test_runtime_gil_status_warns_when_free_threaded_runtime_enables_gil(monkeypatch):
    """free-threaded 运行时退化为 GIL 模式时必须留下明确诊断。"""
    monkeypatch.setattr(lifecycle, "is_free_threaded_runtime", lambda: True)
    monkeypatch.setattr(lifecycle, "is_gil_enabled", lambda: True)
    warning = MagicMock()
    monkeypatch.setattr(lifecycle.logger, "warning", warning)

    lifecycle._log_runtime_gil_status()

    warning.assert_called_once()
    assert "已启用 GIL" in warning.call_args.args[0]


def test_lifespan_validation_failure_does_not_clear_outer_loop_owner(monkeypatch):
    """当前生命周期尚未取得 owner 时，启动失败不得清理外层登记。"""
    _patch_lifespan(monkeypatch)
    monkeypatch.setattr(
        lifecycle,
        "validate_process_topology",
        MagicMock(side_effect=RuntimeError("invalid topology")),
    )

    async def run_lifespan():
        async with lifecycle.lifespan(FastAPI()):
            pass

    with pytest.raises(RuntimeError, match="invalid topology"):
        asyncio.run(run_lifespan())

    lifecycle.main_loop_registry.register.assert_not_called()
    lifecycle.main_loop_registry.release.assert_not_called()


def test_lifespan_settles_plugin_handlers_before_legacy_hooks(monkeypatch) -> None:
    """整理尾事件必须在 handler 停用后结算，并先于旧插件停机 hook。"""
    shutdown_steps = _patch_lifespan(monkeypatch)
    order: list[str] = []
    for name in (
        "stop_transfer",
        "quiesce_plugins",
        "settle_events",
        "quiesce_plugin_services",
        "drain_events",
        "finalize_plugins",
    ):
        shutdown_steps[name].side_effect = (
            lambda current=name: order.append(current) or True
        )

    async def run_lifespan() -> None:
        """运行一个完整的隔离生命周期。"""
        async with lifecycle.lifespan(FastAPI()):
            pass

    asyncio.run(run_lifespan())

    assert order == [
        "stop_transfer",
        "quiesce_plugins",
        "settle_events",
        "quiesce_plugin_services",
        "drain_events",
        "finalize_plugins",
    ]


_ORDERED_SHUTDOWN_STEPS = (
    "stop_plugin_monitor",
    "backup_plugins",
    "stop_workflow",
    "stop_monitor",
    "stop_scheduler",
    "stop_agent",
    "stop_transfer",
    "quiesce_plugins",
    "settle_events",
    "quiesce_plugin_services",
    "drain_events",
    "finalize_plugins",
    "message",
    "stop_modules",
    "close_http",
)


def test_scheduler_shutdown_callback_is_awaited_on_lifecycle_loop(monkeypatch) -> None:
    """定时器关闭必须在生命周期主循环等待异步句柄收口。"""
    awaited = False

    async def stop_scheduler_async() -> None:
        """记录 manifest 回调返回的协程已被生命周期等待。"""
        nonlocal awaited
        awaited = True

    monkeypatch.setattr(lifecycle, "stop_scheduler", stop_scheduler_async)
    component = next(
        item
        for item in lifecycle.build_lifecycle_components(FastAPI())
        if item.name == "定时器"
    )

    assert component.stop is stop_scheduler_async
    assert asyncio.run(
        lifecycle.run_shutdown_step("定时器", component.stop)
    ) is True
    assert awaited is True


@pytest.mark.parametrize(
    "failing_step",
    (
        "stop_plugin_monitor",
        "stop_workflow",
        "stop_monitor",
        "stop_scheduler",
        "stop_agent",
        "stop_transfer",
        "quiesce_plugins",
        "settle_events",
        "quiesce_plugin_services",
        "drain_events",
        "finalize_plugins",
    ),
)
def test_lifespan_stops_releasing_dependencies_when_owner_does_not_converge(
    monkeypatch,
    failing_step,
):
    """关键 owner 未收敛时不得关闭仍被活任务使用的后续依赖。"""
    shutdown_steps = _patch_lifespan(monkeypatch)
    shutdown_steps[failing_step].return_value = False
    failed_index = _ORDERED_SHUTDOWN_STEPS.index(failing_step)
    completed_steps = _ORDERED_SHUTDOWN_STEPS[: failed_index + 1]
    blocked_steps = _ORDERED_SHUTDOWN_STEPS[failed_index + 1 :]

    async def run_lifespan():
        """启动并关闭隔离后的应用生命周期。"""
        async with lifecycle.lifespan(FastAPI()):
            pass

    asyncio.run(run_lifespan())

    for name in (*completed_steps, "logger"):
        _assert_completed_once(shutdown_steps[name])
    for name in blocked_steps:
        shutdown_steps[name].assert_not_called()


def test_task_registry_nonconvergence_blocks_all_dependency_release(monkeypatch):
    """最前置任务 owner 超时后不得继续释放插件、模块或 HTTP 依赖。"""
    shutdown_steps = _patch_lifespan(monkeypatch)
    shutdown = AsyncMock(return_value=False)
    monkeypatch.setattr(lifecycle.TaskRegistry, "shutdown", shutdown)
    app = FastAPI()

    async def run_lifespan() -> None:
        """运行后台登记器无法收敛的隔离生命周期。"""
        async with lifecycle.lifespan(app):
            pass

    asyncio.run(run_lifespan())

    shutdown.assert_awaited_once_with(timeout_seconds=30.0)
    for name, step in shutdown_steps.items():
        if name == "logger":
            _assert_completed_once(step)
        else:
            step.assert_not_called()
    assert isinstance(app.state.task_registry, lifecycle.TaskRegistry)


def test_closed_task_registry_rejects_late_shutdown_tasks(monkeypatch) -> None:
    """首屏障完成后，后续 stop hook 的晚到任务不得落回默认登记器。"""
    _patch_lifespan(monkeypatch)
    app = FastAPI()

    async def run_lifespan() -> None:
        """结束完整 lifespan 后验证当前发布的仍是已封口登记器。"""
        async with lifecycle.lifespan(app):
            pass
        registry = get_task_registry()
        assert registry is app.state.task_registry
        with pytest.raises(RuntimeError, match="正在关闭"):
            registry.create(asyncio.sleep(0), owner="shutdown.late_task")

    asyncio.run(run_lifespan())


def test_plugin_settlement_cannot_bypass_task_registry_shutdown_budget(
    monkeypatch,
) -> None:
    """未收敛 settlement 必须交给首屏障判定，lifespan 不得提前无界等待。"""
    shutdown_steps = _patch_lifespan(monkeypatch)
    shutdown = AsyncMock(return_value=False)
    monkeypatch.setattr(lifecycle.TaskRegistry, "shutdown", shutdown)
    started = asyncio.Event()
    release = asyncio.Event()

    async def settle_plugins() -> None:
        """模拟停机时仍未结束的插件同步任务。"""
        started.set()
        await release.wait()

    lifecycle.init_extra.side_effect = settle_plugins

    async def run_lifespan() -> None:
        """仅对关闭阶段计时，避免覆盖率启动开销污染停机预算断言。"""
        lifespan_context = lifecycle.lifespan(FastAPI())
        await lifespan_context.__aenter__()
        try:
            await started.wait()
            await asyncio.wait_for(
                lifespan_context.__aexit__(None, None, None),
                timeout=0.5,
            )
        finally:
            release.set()
            await asyncio.sleep(0)

    asyncio.run(run_lifespan())

    shutdown.assert_awaited_once_with(timeout_seconds=30.0)
    for name, step in shutdown_steps.items():
        if name == "logger":
            _assert_completed_once(step)
        else:
            step.assert_not_called()


def test_lifespan_waits_for_uncancellable_plugin_settlement_before_shutdown(
    monkeypatch,
):
    """已进入同步 I/O 的 settlement 必须真实结束，才能备份和释放资源。"""
    shutdown_steps = _patch_lifespan(monkeypatch)
    order = []
    shutdown_steps["backup_plugins"].side_effect = lambda: order.append("backup")

    async def run_lifespan():
        started = asyncio.Event()
        release = asyncio.Event()

        async def settle_plugins():
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                # 模拟 run_in_threadpool_to_completion：外层取消只能封住新工作，
                # 已进入同步插件源码/依赖修改的调用仍持有 owner 到真实终态。
                await release.wait()
            order.append("settled")

        lifecycle.init_extra.side_effect = settle_plugins
        async with lifecycle.lifespan(FastAPI()):
            await started.wait()
            asyncio.get_running_loop().call_later(0.02, release.set)

    asyncio.run(run_lifespan())

    assert order[:2] == ["settled", "backup"]


def test_lifespan_configures_plugin_runtime_and_services_in_dependency_order(monkeypatch):
    """插件 Runtime、模块持久化和应用服务必须按依赖顺序装配。"""
    shutdown_steps = _patch_lifespan(monkeypatch)
    order = []
    lifecycle.configure_plugin_runtime_services.side_effect = lambda: order.append("runtime")
    lifecycle.init_modules.side_effect = lambda: order.append("modules")
    lifecycle.configure_plugin_services.side_effect = lambda: order.append("services")
    lifecycle.get_plugin_installation_recovery.return_value.replay.side_effect = (
        lambda: order.append("replay")
    )
    lifecycle.SystemChain.return_value.restore_plugins.side_effect = (
        lambda: order.append("restore")
    )

    async def run_lifespan():
        async with lifecycle.lifespan(FastAPI()):
            pass

    asyncio.run(run_lifespan())

    assert order == ["runtime", "modules", "services", "replay", "restore"]
    _assert_completed_once(shutdown_steps["close_http"])


def test_lifespan_safe_mode_skips_optional_runtime(monkeypatch):
    """安全模式只启动基础模块，并跳过插件及可选后台服务。"""
    shutdown_steps = _patch_lifespan(monkeypatch)
    monkeypatch.setattr(runtime_settings, "MOVIEPILOT_SAFE_MODE", True)

    async def run_lifespan():
        async with lifecycle.lifespan(FastAPI()):
            pass

    asyncio.run(run_lifespan())

    lifecycle.init_modules.assert_awaited_once_with()
    lifecycle.prepare_database_component.assert_called_once()
    for name in (
        "init_plugins",
        "init_scheduler",
        "init_monitor",
        "replay_pending_transfers",
        "init_command",
        "init_workflow",
    ):
        getattr(lifecycle, name).assert_not_called()
    for name in (
        "backup_plugins",
        "stop_workflow",
        "stop_monitor",
        "stop_scheduler",
        "stop_plugin_monitor",
        "quiesce_plugins",
        "settle_events",
        "quiesce_plugin_services",
        "finalize_plugins",
    ):
        shutdown_steps[name].assert_not_called()
    _assert_completed_once(shutdown_steps["stop_modules"])
    _assert_completed_once(shutdown_steps["stop_agent"])
    _assert_completed_once(shutdown_steps["stop_transfer"])
    _assert_completed_once(shutdown_steps["drain_events"])
    _assert_completed_once(shutdown_steps["close_http"])
    _assert_completed_once(shutdown_steps["logger"])


@pytest.mark.asyncio
async def test_event_drain_does_not_materialize_manager(monkeypatch) -> None:
    """模块尚未创建事件总线时，停机屏障应直接收敛而不反向构造。"""
    event_manager_type = MagicMock()
    event_manager_type.get_existing_instance.return_value = None
    monkeypatch.setattr(modules_initializer, "EventManager", event_manager_type)

    assert await modules_initializer.drain_events() is True
    event_manager_type.get_existing_instance.assert_called_once_with()
    event_manager_type.assert_not_called()


@pytest.mark.asyncio
async def test_event_settlement_keeps_tail_event_admission_open(monkeypatch) -> None:
    """中间结算只等待在途 handler，不得提前封死旧 hook 的尾事件。"""
    event_manager = MagicMock()
    event_manager.drain_async = AsyncMock(return_value=True)
    event_manager_type = MagicMock()
    event_manager_type.get_existing_instance.return_value = event_manager
    monkeypatch.setattr(modules_initializer, "EventManager", event_manager_type)

    assert await modules_initializer.settle_events() is True
    event_manager.drain_async.assert_awaited_once_with(seal=False)


def test_lifecycle_manifest_declares_normal_and_safe_mode_order() -> None:
    """组件清单应显式冻结依赖、模式、启动/关闭顺序和超时预算。"""
    app = FastAPI()
    normal = lifecycle.get_lifecycle_manifest(app, safe_mode=False)
    safe = lifecycle.get_lifecycle_manifest(app, safe_mode=True)

    normal_start = [
        item["name"]
        for item in sorted(
            (entry for entry in normal if entry["start_order"] is not None),
            key=lambda entry: entry["start_order"],
        )
    ]
    normal_stop = [
        item["name"]
        for item in sorted(
            (entry for entry in normal if entry["stop_order"] is not None),
            key=lambda entry: entry["stop_order"],
        )
    ]
    safe_names = {item["name"] for item in safe}

    assert normal_start == [
        "文件日志",
        "后台任务登记器",
        "数据库准备",
        "HTTP 基础能力",
        "站点访问端口",
        "Chain 外部端口",
        "Chain 网络端口",
        "领域依赖装配",
        "数据库引擎预热",
        "数据库连接预算",
        "路由",
        "插件运行时装配",
        "模块服务",
        "插件服务装配",
        "消息队列",
        "插件备份恢复",
        "插件",
        "定时器",
        "监控器",
        "待处理整理回放",
        "命令服务",
        "工作流",
        "插件同步与启动收尾",
    ]
    assert normal_stop == [
        "停止信号",
        "后台任务登记器",
        "插件变更监控",
        "插件备份",
        "工作流",
        "命令服务",
        "监控器",
        "定时器",
        "AI智能体会话",
        "整理后台服务",
        "插件事件入口",
        "事件尾任务结算",
        "插件后台服务",
        "事件投递屏障",
        "插件",
        "消息队列",
        "模块服务",
        "Chain 网络端口",
        "Chain 外部端口",
        "站点访问端口",
        "HTTP 基础能力",
        "文件日志",
    ]
    assert safe_names == {
        "文件日志",
        "后台任务登记器",
        "数据库准备",
        "HTTP 基础能力",
        "站点访问端口",
        "Chain 外部端口",
        "Chain 网络端口",
        "领域依赖装配",
        "数据库引擎预热",
        "数据库连接预算",
        "路由",
        "插件运行时装配",
        "模块服务",
        "插件服务装配",
        "消息队列",
        "AI智能体会话",
        "整理后台服务",
        "事件投递屏障",
        "停止信号",
        "插件同步与启动收尾",
    }
    assert all(item["start_failure"] == "fail_fast" for item in normal)
    assert {
        item["name"]
        for item in normal
        if item["stop_failure"] == "fail_fast"
    } == {
        "插件变更监控",
        "后台任务登记器",
        "工作流",
        "监控器",
        "定时器",
        "AI智能体会话",
        "整理后台服务",
        "插件事件入口",
        "事件尾任务结算",
        "插件后台服务",
        "事件投递屏障",
        "插件",
        "消息队列",
        "文件日志",
    }
    assert all(
        item["stop_failure"] == "continue"
        for item in normal
        if item["name"]
        not in {
            "插件变更监控",
            "后台任务登记器",
            "工作流",
            "监控器",
            "定时器",
            "AI智能体会话",
            "整理后台服务",
            "插件事件入口",
            "事件尾任务结算",
            "插件后台服务",
            "事件投递屏障",
            "插件",
            "消息队列",
            "文件日志",
        }
    )
    assert all(
        item["start_timeout_seconds"] or item["stop_timeout_seconds"]
        for item in normal
    )


def test_startup_step_records_duration_without_changing_result(monkeypatch):
    """启动阶段计时必须保留返回值，并输出稳定的阶段名称和毫秒耗时。"""
    perf_counter = MagicMock(side_effect=[10.0, 10.125])
    logger_info = MagicMock()
    monkeypatch.setattr(lifecycle.time, "perf_counter", perf_counter)
    monkeypatch.setattr(lifecycle.logger, "info", logger_info)

    result = asyncio.run(
        lifecycle.run_startup_step("契约测试", lambda: "ready")
    )

    assert result == "ready"
    logger_info.assert_called_once_with(
        "启动%s完成，耗时=%.2fms",
        "契约测试",
        125.0,
    )


def test_lifespan_creates_global_async_engine_at_startup(monkeypatch):
    """启动期必须把全局异步引擎建出来一次，让异步侧恢复 fail-fast

    引擎改为惰性创建后，启动路径只碰得到同步引擎（init_db 建表），异步驱动没装、
    异步 URL 拼错这类问题会一路推迟到第一个异步查询——用户拿到 500、调度任务静默死掉，
    而不是启动就崩。create_async_engine 只校验 URL 与驱动导入、不建立连接，代价可以忽略。
    """
    _patch_lifespan(monkeypatch)
    created = []
    monkeypatch.setattr(lifecycle, "get_global_async_engine",
                        lambda: created.append(1) or MagicMock())

    async def run_lifespan():
        async with lifecycle.lifespan(FastAPI()):
            pass

    asyncio.run(run_lifespan())

    assert created, "启动期未创建全局异步引擎，异步侧的驱动/URL 错误会推迟到运行期才暴露"


def test_lifespan_creates_sync_engine_at_startup(monkeypatch):
    """启动期也必须把同步引擎建出来一次，把首次创建钉在单线程期

    数据库准备已统一进入 lifespan，所有受支持 ASGI 入口都会先由 init_db() 创建同步引擎；
    随后的显式预热仍用于冻结顺序契约，确保同步/异步引擎都早于 Router、Module 和后台线程。
    """
    _patch_lifespan(monkeypatch)
    created = []
    monkeypatch.setattr(lifecycle, "get_engine",
                        lambda: created.append(1) or MagicMock())

    async def run_lifespan():
        async with lifecycle.lifespan(FastAPI()):
            pass

    asyncio.run(run_lifespan())

    assert created, "启动期未预热同步引擎，首次创建会退到已经放出上百个线程的运行期"


def test_lifespan_warms_engines_before_any_initializer(monkeypatch):
    """两个引擎的预热必须排在 init_routers / init_modules 之前

    排在后面时，预热失败会把已经初始化好的模块晾在那里：lifespan 的 try/finally 关停块
    要到 yield 处才开始，在它之前抛异常，stop_modules() 根本没有机会执行。
    """
    _patch_lifespan(monkeypatch)
    calls = []
    monkeypatch.setattr(lifecycle, "get_engine", lambda: calls.append("sync_engine"))
    monkeypatch.setattr(lifecycle, "get_global_async_engine",
                        lambda: calls.append("async_engine"))
    monkeypatch.setattr(
        lifecycle,
        "init_routers",
        lambda _app, _api_prefix: calls.append("init_routers"),
    )
    async def _init_modules():
        """init_modules 在 v3 是协程，桩也必须可 await。"""
        calls.append("init_modules")

    monkeypatch.setattr(lifecycle, "init_modules", _init_modules)

    async def run_lifespan():
        async with lifecycle.lifespan(FastAPI()):
            pass

    asyncio.run(run_lifespan())

    # 不钉同步/异步两者之间的先后：那一层顺序无所谓，要紧的是它们都在 init_* 之前
    assert set(calls[:2]) == {"sync_engine", "async_engine"}, f"引擎预热没有排在最前面：{calls}"
    assert calls[2:] == ["init_routers", "init_modules"], f"初始化顺序被打乱：{calls}"


def test_lifespan_fails_fast_when_async_engine_cannot_be_built(monkeypatch):
    """异步引擎建不起来必须让启动直接失败，不能吞掉继续跑

    吞掉等于把 fail-fast 又还回去了：进程起来了、健康检查是绿的，只有异步请求在报错。
    """
    _patch_lifespan(monkeypatch)

    def _boom():
        """模拟异步驱动缺失。"""
        raise RuntimeError("no async driver")

    monkeypatch.setattr(lifecycle, "get_global_async_engine", _boom)

    async def run_lifespan():
        async with lifecycle.lifespan(FastAPI()):
            pass

    with pytest.raises(RuntimeError, match="no async driver"):
        asyncio.run(run_lifespan())

    lifecycle.main_loop_registry.release.assert_called_once_with(
        lifecycle.main_loop_registry.register.return_value
    )
    # 失败要发生在任何东西被初始化之前，否则模块起来了却没人关：关停块在 yield 处才开始
    lifecycle.init_routers.assert_not_called()
    lifecycle.init_modules.assert_not_called()


def test_uvicorn_signal_publishes_stop_before_server_exit(monkeypatch):
    """Uvicorn 接管系统信号时必须先发布协作停止标志"""
    from app import main

    calls = []
    monkeypatch.setattr(
        main.runtime_stop_state,
        "stop_system",
        lambda: calls.append("stop"),
    )
    monkeypatch.setattr(
        main.uvicorn.Server,
        "handle_exit",
        lambda _self, _sig, _frame: calls.append("uvicorn"),
    )

    server = object.__new__(main.MoviePilotServer)
    server.handle_exit(signal.SIGTERM, None)

    assert calls == ["stop", "uvicorn"]


def test_application_preserves_stop_requested_before_startup(monkeypatch):
    """启动流程不能清除初始化前已经发布的退出请求"""
    from app import main

    stop_event = threading.Event()
    stop_event.set()
    monkeypatch.setattr(main.runtime_stop_state, "_system_event", stop_event)
    calls = []
    monkeypatch.setattr(
        main.signal,
        "signal",
        lambda *_args: calls.append("signal"),
    )
    monkeypatch.setattr(main, "start_tray", lambda: calls.append("tray"))
    monkeypatch.setattr(main, "run_api_server", lambda: calls.append("server"))

    main.run_application()

    assert stop_event.is_set()
    assert calls == [
        "signal",
        "signal",
        "tray",
        "server",
    ]


def test_asgi_and_main_entrypoints_share_the_same_app_instance():
    """ASGI 工厂入口与主程序入口必须暴露同一个 FastAPI 实例。"""
    from app import factory, main

    assert main.app is factory.app


def test_lifespan_does_not_yield_after_migration_failure(monkeypatch):
    """数据库迁移失败时 lifespan 必须 fail-fast 且不得发布 ready。"""
    migration_error = RuntimeError("migration failed")
    _patch_lifespan(monkeypatch)
    monkeypatch.setattr(
        lifecycle,
        "prepare_database_component",
        MagicMock(side_effect=migration_error),
    )
    app = FastAPI()

    async def run_lifespan():
        async with lifecycle.lifespan(app):
            pytest.fail("数据库迁移失败后不应进入服务阶段")

    with pytest.raises(RuntimeError) as raised:
        asyncio.run(run_lifespan())

    assert raised.value is migration_error
    assert app.state.moviepilot_health.is_ready is False
    assert app.state.moviepilot_health.phase.value == "failed"


def test_lifespan_cleans_started_owners_after_late_startup_failure(monkeypatch):
    """后段启动失败时应按同一停机策略回收已启动及部分启动的 owner。"""
    shutdown_steps = _patch_lifespan(monkeypatch)
    startup_error = RuntimeError("command startup failed")
    lifecycle.init_command.side_effect = startup_error
    app = FastAPI()

    async def run_lifespan() -> None:
        """运行一个在命令服务阶段失败的隔离生命周期。"""
        async with lifecycle.lifespan(app):
            pytest.fail("命令服务启动失败后不应发布运行态")

    with pytest.raises(RuntimeError) as raised:
        asyncio.run(run_lifespan())

    assert raised.value is startup_error
    # 停止信号是无依赖的 stop-only owner：启动失败清理同样要先发出停机通知，
    # 让仍在运行的后台任务尽早感知进程即将退出。
    lifecycle.runtime_stop_state.stop_system.assert_called_once_with()
    for name in (
        "stop_plugin_monitor",
        "backup_plugins",
        "stop_monitor",
        "stop_scheduler",
        "stop_agent",
        "stop_transfer",
        "quiesce_plugins",
        "settle_events",
        "quiesce_plugin_services",
        "drain_events",
        "finalize_plugins",
        "stop_modules",
        "close_http",
    ):
        _assert_completed_once(shutdown_steps[name])
    shutdown_steps["stop_workflow"].assert_not_called()
    _assert_completed_once(shutdown_steps["message"])
    _assert_completed_once(shutdown_steps["logger"])
    assert isinstance(app.state.task_registry, lifecycle.TaskRegistry)
    assert get_task_registry() is app.state.task_registry
    assert app.state.moviepilot_health.phase.value == "failed"


def test_startup_failure_cleanup_honors_transfer_fail_fast(monkeypatch):
    """启动失败清理中整理 owner 未收敛时也不得继续释放插件和模块。"""
    shutdown_steps = _patch_lifespan(monkeypatch)
    lifecycle.init_command.side_effect = RuntimeError("command startup failed")
    shutdown_steps["stop_transfer"].return_value = False

    async def run_lifespan() -> None:
        """运行后段失败且整理线程无法收敛的隔离生命周期。"""
        async with lifecycle.lifespan(FastAPI()):
            pytest.fail("命令服务启动失败后不应发布运行态")

    with pytest.raises(RuntimeError, match="command startup failed"):
        asyncio.run(run_lifespan())

    for name in (
        "stop_plugin_monitor",
        "backup_plugins",
        "stop_monitor",
        "stop_scheduler",
        "stop_agent",
        "stop_transfer",
    ):
        _assert_completed_once(shutdown_steps[name])
    for name in (
        "quiesce_plugins",
        "settle_events",
        "quiesce_plugin_services",
        "drain_events",
        "finalize_plugins",
        "stop_modules",
        "close_http",
    ):
        shutdown_steps[name].assert_not_called()
    shutdown_steps["stop_workflow"].assert_not_called()


def test_uvicorn_preserves_stop_requested_before_serve(monkeypatch):
    """Uvicorn 启动不能清除数据库初始化阶段已经发布的停止请求"""
    from app import main

    stop_event = threading.Event()
    monkeypatch.setattr(main.runtime_stop_state, "_system_event", stop_event)
    main.runtime_stop_state.stop_system()

    async def serve(_self, sockets=None):
        assert main.runtime_stop_state.is_system_stopped

    monkeypatch.setattr(main.uvicorn.Server, "serve", serve)
    server = object.__new__(main.MoviePilotServer)
    asyncio.run(server.serve())


@pytest.mark.parametrize("endpoint_name", ["restart_system", "install_system_update"])
@pytest.mark.parametrize(
    "initially_stopped",
    [False, True],
    ids=["running", "stopping"],
)
def test_restart_endpoint_failure_preserves_stop_state(
    monkeypatch,
    endpoint_name,
    initially_stopped,
):
    """重启或更新安装失败不能发布或撤销停止请求"""
    from app.api.endpoints import system

    stop_event = threading.Event()
    if initially_stopped:
        stop_event.set()
    monkeypatch.setattr(system.runtime_stop_state, "_system_event", stop_event)
    monkeypatch.setattr(
        "app.startup.composition.system.SystemHelper.can_restart",
        MagicMock(return_value=True),
    )
    monkeypatch.setattr(
        "app.startup.composition.system.SystemHelper.restart",
        MagicMock(return_value=(False, "restart failed")),
    )
    monkeypatch.setattr(
        "app.startup.composition.system.system_update_manager.request_install",
        MagicMock(return_value=(True, "prepared")),
    )
    cancel_install = MagicMock()
    monkeypatch.setattr(
        "app.startup.composition.system.system_update_manager.cancel_install",
        cancel_install,
    )
    runtime = _system_runtime()

    if endpoint_name == "restart_system":
        response = system.restart_system(None, runtime)
    else:
        response = system.install_system_update(None, runtime)

    assert not response.success
    assert stop_event.is_set() is initially_stopped
    if endpoint_name == "install_system_update":
        cancel_install.assert_called_once_with("restart failed")
    else:
        cancel_install.assert_not_called()


def test_upgrade_endpoint_retains_dev_mode_only(monkeypatch):
    """旧升级入口只保留 Dev，Release 必须迁移到后台下载流程。"""
    from app.api.endpoints import system

    monkeypatch.setattr(
        "app.startup.composition.system.SystemHelper.can_restart",
        MagicMock(return_value=True),
    )
    upgrade_dev = MagicMock(return_value=(True, "dev queued"))
    monkeypatch.setattr(
        "app.startup.composition.system.SystemHelper.upgrade_dev", upgrade_dev
    )
    runtime = _system_runtime()

    dev_response = system.upgrade_system("dev", None, runtime)
    release_response = system.upgrade_system("release", None, runtime)
    legacy_default_response = system.upgrade_system(None, None, runtime)

    assert dev_response.success
    assert not release_response.success
    assert not legacy_default_response.success
    assert "update/check" in release_response.message
    upgrade_dev.assert_called_once_with()


def test_command_restart_failure_does_not_publish_stop_request(monkeypatch):
    """命令重启失败时进程仍在运行，不能提前发布停止请求"""
    from app.chain.system import SystemChain
    from app.runtime.config import global_vars

    stop_event = threading.Event()
    monkeypatch.setattr(global_vars, "STOP_EVENT", stop_event)
    monkeypatch.setattr(SystemChain, "backup_plugins", MagicMock())
    restart = MagicMock(return_value=(False, "restart failed"))
    monkeypatch.setattr("app.chain.system.SystemHelper.restart", restart)

    chain = object.__new__(SystemChain)
    chain.restart(channel=None, userid=None)

    restart.assert_called_once_with()
    assert not stop_event.is_set()


def test_stop_modules_continues_after_internal_owner_failure(monkeypatch):
    """模块关闭编排中的单个失败不能阻断其余清理。"""
    dependencies = _patch_module_shutdown_dependencies(monkeypatch)
    dependencies["module"].side_effect = RuntimeError("module failed")

    converged = asyncio.run(modules_initializer.stop_modules())

    assert converged is False
    for dependency in dependencies.values():
        _assert_completed_once(dependency)


def test_stop_modules_propagates_false_without_skipping_later_cleanup(monkeypatch):
    """关闭回调显式返回 False 时不得被转换为整体成功。"""
    dependencies = _patch_module_shutdown_dependencies(monkeypatch)
    dependencies["module"].return_value = False

    converged = asyncio.run(modules_initializer.stop_modules())

    assert converged is False
    for dependency in dependencies.values():
        _assert_completed_once(dependency)


def test_stop_modules_propagates_shared_thread_pool_nonconvergence(monkeypatch):
    """共享线程池仍有活动 Future 时必须由模块服务关闭结果向上暴露。"""
    dependencies = _patch_module_shutdown_dependencies(monkeypatch)
    dependencies["thread"].return_value = False

    converged = asyncio.run(modules_initializer.stop_modules())

    assert converged is False
    for dependency in dependencies.values():
        _assert_completed_once(dependency)


def test_stop_modules_propagates_doh_nonconvergence(monkeypatch):
    """DoH 查询线程未终止时必须由模块服务关闭结果向上暴露。"""
    dependencies = _patch_module_shutdown_dependencies(monkeypatch)
    dependencies["doh"].return_value = False

    converged = asyncio.run(modules_initializer.stop_modules())

    assert converged is False
    for dependency in dependencies.values():
        _assert_completed_once(dependency)


@pytest.mark.asyncio
async def test_stop_modules_keeps_event_loop_responsive_during_sync_owner_wait(
    monkeypatch,
):
    """同步 owner 的有界等待不得占用主事件循环。"""
    dependencies = _patch_module_shutdown_dependencies(monkeypatch)
    release = threading.Event()
    timer = threading.Timer(0.05, release.set)
    dependencies["module"].side_effect = lambda: release.wait(timeout=1.0)

    heartbeat = asyncio.create_task(asyncio.sleep(0.01))
    timer.start()
    try:
        await modules_initializer.stop_modules()
    finally:
        timer.join(timeout=1.0)

    assert heartbeat.done()


def test_stop_modules_drains_web_agent_tasks_before_persistence(monkeypatch):
    """关闭时先收口 Web Agent，再关闭持久化准入和数据库任务。"""
    order = []
    dependencies = _patch_module_shutdown_dependencies(monkeypatch)
    monkeypatch.setattr(
        modules_initializer,
        "shutdown_web_agent_background_tasks",
        AsyncMock(side_effect=lambda: order.append("web-agent")),
    )
    persistence = MagicMock()
    persistence.begin_shutdown = MagicMock(
        side_effect=lambda: order.append("persistence-admission")
    )
    persistence.shutdown = AsyncMock(side_effect=lambda: order.append("persistence"))
    monkeypatch.setattr(
        modules_initializer,
        "get_configured_agent_chat_persistence",
        MagicMock(return_value=persistence),
    )
    database_state = {"active": True}

    async def stop_database_runtime() -> None:
        """模拟生产 worker 关闭后释放组合根句柄。"""
        order.append("database")
        database_state["active"] = False

    monkeypatch.setattr(
        modules_initializer,
        "stop_database_runtime",
        AsyncMock(side_effect=stop_database_runtime),
    )
    monkeypatch.setattr(
        modules_initializer,
        "database_runtime_active",
        lambda: database_state["active"],
    )
    dependencies["reset_module_providers"].side_effect = lambda: order.append(
        "providers"
    ) or True
    dependencies["close_database"].side_effect = lambda: order.append("connection")

    converged = asyncio.run(modules_initializer.stop_modules())

    assert converged is True
    assert order == [
        "web-agent",
        "persistence-admission",
        "persistence",
        "database",
        "providers",
        "connection",
    ]


def test_stop_modules_retains_providers_when_database_worker_remains_active(
    monkeypatch,
) -> None:
    """数据库 worker 未收敛时不得撤销 Provider 或关闭仍被使用的连接。"""
    dependencies = _patch_module_shutdown_dependencies(monkeypatch)
    persistence = MagicMock()
    persistence.begin_shutdown = MagicMock()
    persistence.shutdown = AsyncMock()
    monkeypatch.setattr(
        modules_initializer,
        "get_configured_agent_chat_persistence",
        MagicMock(return_value=persistence),
    )
    monkeypatch.setattr(
        modules_initializer,
        "stop_database_runtime",
        AsyncMock(side_effect=RuntimeError("database busy")),
    )
    monkeypatch.setattr(
        modules_initializer,
        "database_runtime_active",
        lambda: True,
    )

    assert asyncio.run(modules_initializer.stop_modules()) is False
    dependencies["reset_module_providers"].assert_not_called()
    dependencies["close_database"].assert_not_awaited()


def test_reset_module_providers_preserves_reverse_order_after_failure(
    monkeypatch,
) -> None:
    """单个 Provider reset 失败时仍按既定逆序执行全部后续 owner。"""
    calls: list[str] = []

    def fail() -> None:
        """记录失败 owner 后抛出异常。"""
        calls.append("second")
        raise RuntimeError("reset failed")

    monkeypatch.setattr(
        modules_initializer,
        "_module_provider_reset_steps",
        lambda: (
            ("first", lambda: calls.append("first")),
            ("second", fail),
            ("third", lambda: calls.append("third")),
        ),
    )

    assert modules_initializer.reset_module_providers() is False
    assert calls == ["first", "second", "third"]


@pytest.mark.asyncio
async def test_stop_modules_cancellation_does_not_skip_database_runtime_cleanup(
    monkeypatch,
):
    """模块关闭收到取消请求后仍应继续收口数据库 worker。"""
    started = asyncio.Event()

    async def blocked_web_agent_shutdown():
        started.set()
        await asyncio.Event().wait()

    _patch_module_shutdown_dependencies(monkeypatch)
    monkeypatch.setattr(
        modules_initializer,
        "shutdown_web_agent_background_tasks",
        blocked_web_agent_shutdown,
    )
    monkeypatch.setattr(
        modules_initializer,
        "wait_web_agent_background_tasks",
        AsyncMock(),
    )
    persistence = MagicMock()
    persistence.begin_shutdown = MagicMock()
    persistence.shutdown = AsyncMock()
    monkeypatch.setattr(
        modules_initializer,
        "get_configured_agent_chat_persistence",
        MagicMock(return_value=persistence),
    )
    database_worker_stopped = asyncio.Event()
    database_state = {"active": True}

    def mark_database_worker_stopped() -> None:
        """记录取消路径仍执行了 worker 关闭调用。"""
        database_worker_stopped.set()

    stop_database_runtime = AsyncMock(side_effect=mark_database_worker_stopped)
    monkeypatch.setattr(modules_initializer, "stop_database_runtime", stop_database_runtime)
    monkeypatch.setattr(
        modules_initializer,
        "database_runtime_active",
        lambda: database_state["active"],
    )

    shutdown = asyncio.create_task(modules_initializer.stop_modules())
    await started.wait()
    shutdown.cancel()
    completed = await shutdown

    assert completed is False
    assert database_worker_stopped.is_set()
    stop_database_runtime.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_shutdown_timeout_has_hard_bound_for_nonconverging_cleanup() -> None:
    """关闭收尾不响应取消时，生命周期调用仍必须在预算内返回。"""
    started = asyncio.Event()
    cancel_requested = asyncio.Event()
    release = asyncio.Event()
    settled = asyncio.Event()

    async def nonconverging_shutdown() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancel_requested.set()
            await release.wait()
            settled.set()
            raise

    started_at = asyncio.get_running_loop().time()
    shutdown = asyncio.create_task(
        lifecycle.run_shutdown_step(
            "不可收敛阶段",
            nonconverging_shutdown,
            timeout_seconds=0.01,
        )
    )
    await started.wait()
    completed = await shutdown

    elapsed = asyncio.get_running_loop().time() - started_at
    assert completed is False
    assert elapsed < 0.2
    await asyncio.wait_for(cancel_requested.wait(), timeout=0.2)
    assert not settled.is_set()

    release.set()
    await asyncio.wait_for(settled.wait(), timeout=0.2)


@pytest.mark.asyncio
async def test_shutdown_step_bounds_sync_owner_without_blocking_event_loop() -> None:
    """同步 owner 超时后应及时返回，并继续持有 worker 直至真实终态。"""
    started = threading.Event()
    release = threading.Event()
    settled = threading.Event()

    def blocking_shutdown() -> None:
        started.set()
        release.wait(timeout=1.0)
        settled.set()

    heartbeat = asyncio.create_task(asyncio.sleep(0.01))
    shutdown = asyncio.create_task(
        lifecycle.run_shutdown_step(
            "同步阻塞 owner",
            lifecycle.offload_blocking_callback(blocking_shutdown),
            timeout_seconds=0.02,
        )
    )
    assert await asyncio.to_thread(started.wait, 0.2)
    started_at = asyncio.get_running_loop().time()
    completed = await shutdown

    assert completed is False
    assert asyncio.get_running_loop().time() - started_at < 0.2
    assert heartbeat.done()
    assert not settled.is_set()

    release.set()
    assert await asyncio.to_thread(settled.wait, 0.2)


@pytest.mark.asyncio
async def test_shutdown_step_calls_awaitable_wrapper_on_event_loop() -> None:
    """普通 callable 可在主循环构造并返回需要等待的异步结果。"""
    loop = asyncio.get_running_loop()

    def shutdown_wrapper() -> asyncio.Task[None]:
        assert asyncio.get_running_loop() is loop
        return loop.create_task(asyncio.sleep(0))

    assert await lifecycle.run_shutdown_step(
        "异步包装 owner",
        shutdown_wrapper,
    ) is True


@pytest.mark.asyncio
async def test_shutdown_step_reports_explicit_nonconvergence() -> None:
    """同步和异步 owner 显式返回 False 时都必须向生命周期传播失败。"""

    async def async_nonconverging_shutdown() -> bool:
        """模拟已经完成等待但仍持有资源的异步关闭入口。"""
        return False

    assert await lifecycle.run_shutdown_step(
        "同步 owner",
        lambda: False,
    ) is False
    assert await lifecycle.run_shutdown_step(
        "异步 owner",
        async_nonconverging_shutdown,
    ) is False
    assert await lifecycle.run_shutdown_step(
        "已收敛 owner",
        lambda: None,
    ) is True


def _patch_module_shutdown_dependencies(monkeypatch) -> dict:
    """替换 stop_modules 的资源所有者，避免测试启动真实后台服务"""
    dependencies = {}
    for name, method_name in (
        ("ModuleManager", "shutdown"),
        ("EventManager", "stop_async"),
        ("ThreadHelper", "shutdown"),
        ("RedisHelper", "close"),
    ):
        instance = MagicMock()
        setattr(instance, method_name, MagicMock())
        instance_type = MagicMock(return_value=instance)
        instance_type.get_existing_instance.return_value = instance
        monkeypatch.setattr(modules_initializer, name, instance_type)
        key = name.removesuffix("Helper").removesuffix("Manager").lower()
        dependencies[key] = getattr(instance, method_name)

    stop_doh_composition = MagicMock()
    monkeypatch.setattr(
        modules_initializer,
        "stop_doh_composition",
        stop_doh_composition,
    )
    dependencies["doh"] = stop_doh_composition

    for name in (
        "close_browser_sessions",
        "stop_frontend",
        "clear_temp",
    ):
        dependency = MagicMock()
        monkeypatch.setattr(modules_initializer, name, dependency)
        dependencies[name] = dependency

    close_image_proxy_block_log_coalescer = AsyncMock()
    monkeypatch.setattr(
        modules_initializer,
        "close_image_proxy_block_log_coalescer",
        close_image_proxy_block_log_coalescer,
    )
    dependencies["close_image_proxy_block_log_coalescer"] = (
        close_image_proxy_block_log_coalescer
    )

    stop_managed_resources = AsyncMock()
    monkeypatch.setattr(
        modules_initializer,
        "stop_managed_resources",
        stop_managed_resources,
    )
    dependencies["stop_managed_resources"] = stop_managed_resources

    async_redis = MagicMock()
    async_redis.close = AsyncMock()
    async_redis_type = MagicMock(return_value=async_redis)
    async_redis_type.get_existing_instance.return_value = async_redis
    monkeypatch.setattr(modules_initializer, "AsyncRedisHelper", async_redis_type)
    dependencies["async_redis"] = async_redis.close
    close_database = AsyncMock()
    monkeypatch.setattr(modules_initializer, "close_database", close_database)
    dependencies["close_database"] = close_database
    reset_module_providers = MagicMock(return_value=True)
    monkeypatch.setattr(
        modules_initializer,
        "reset_module_providers",
        reset_module_providers,
    )
    dependencies["reset_module_providers"] = reset_module_providers
    return dependencies


def test_browser_sessions_close_before_managed_resources(monkeypatch) -> None:
    """显示等宿主资源必须晚于浏览器会话释放，避免存活上下文失去依赖。"""
    calls: list[str] = []
    dependencies = _patch_module_shutdown_dependencies(monkeypatch)
    dependencies["close_browser_sessions"].side_effect = lambda: calls.append("browser")

    async def stop_resources() -> None:
        calls.append("resources")

    dependencies["stop_managed_resources"].side_effect = stop_resources

    asyncio.run(modules_initializer.stop_modules())

    assert calls == ["browser", "resources"]


def test_module_shutdown_waits_for_image_proxy_log_coalescer(monkeypatch) -> None:
    """模块关闭必须等待图片安全日志的在途聚合任务收口。"""
    dependencies = _patch_module_shutdown_dependencies(monkeypatch)

    asyncio.run(modules_initializer.stop_modules())

    dependencies["close_image_proxy_block_log_coalescer"].assert_awaited_once_with()


def test_shared_http_close_waits_for_real_lru_eviction(monkeypatch):
    """最终 HTTP 关闭必须等待真实 LRU 淘汰任务并消费其异常"""

    class FakeTransport:
        created = []

        def __init__(self, **_kwargs):
            self.close_started = asyncio.Event()
            self.release_close = asyncio.Event()
            self.closed = False
            self.fail_on_close = not self.created
            if not self.fail_on_close:
                self.release_close.set()
            self.created.append(self)

        async def aclose(self):
            self.close_started.set()
            await self.release_close.wait()
            self.closed = True
            if self.fail_on_close:
                raise RuntimeError("eviction close failed")

    monkeypatch.setattr(http_utils, "_MAX_SHARED_TRANSPORTS_PER_LOOP", 1)
    monkeypatch.setattr(http_utils.httpx2, "AsyncHTTPTransport", FakeTransport)
    async def run_test():
        transport_kwargs = {
            "proxy": None,
            "verify": True,
            "http2": False,
            "max_keepalive_connections": 1,
            "max_connections": 1,
        }
        evicted_transport = http_utils._get_shared_async_transport(
            **transport_kwargs,
            keepalive_expiry=1,
        )
        active_transport = http_utils._get_shared_async_transport(
            **transport_kwargs,
            keepalive_expiry=2,
        )
        await asyncio.wait_for(evicted_transport.close_started.wait(), timeout=1)

        loop = asyncio.get_running_loop()
        with http_utils._shared_async_transports_lock:
            eviction_tasks = [
                task
                for task in http_utils._pending_eviction_tasks
                if task.get_loop() is loop
            ]
        assert len(eviction_tasks) == 1

        close_task = asyncio.create_task(http_utils.aclose_shared_async_transports())
        await asyncio.sleep(0)
        try:
            assert not close_task.done()
            evicted_transport.release_close.set()
            await close_task
            await asyncio.sleep(0)
            assert eviction_tasks[0].done()
            assert isinstance(eviction_tasks[0].exception(), RuntimeError)
            assert evicted_transport.closed
            assert active_transport.closed
            with http_utils._shared_async_transports_lock:
                assert not any(
                    task.get_loop() is loop
                    for task in http_utils._pending_eviction_tasks
                )
        finally:
            evicted_transport.release_close.set()
            active_transport.release_close.set()
            await asyncio.gather(close_task, return_exceptions=True)
            await http_utils.aclose_shared_async_transports()

    asyncio.run(run_test())

def test_shared_http_close_ignores_eviction_from_other_loop():
    """当前事件循环关闭不能等待其他循环持有的淘汰任务"""
    ready = threading.Event()
    release = threading.Event()
    failures = []
    state = {}

    def run_foreign_loop():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def delayed_close():
            while not release.is_set():
                await asyncio.sleep(0.01)

        task = loop.create_task(delayed_close())
        state["task"] = task
        with http_utils._shared_async_transports_lock:
            http_utils._pending_eviction_tasks.add(task)
        task.add_done_callback(http_utils._discard_pending_eviction_task)
        ready.set()
        try:
            loop.run_until_complete(task)
            loop.run_until_complete(asyncio.sleep(0))
        except BaseException as err:
            failures.append(err)
        finally:
            with http_utils._shared_async_transports_lock:
                http_utils._pending_eviction_tasks.discard(task)
            loop.close()

    thread = threading.Thread(target=run_foreign_loop)
    thread.start()
    try:
        assert ready.wait(timeout=2)
        asyncio.run(http_utils.aclose_shared_async_transports())
        assert thread.is_alive()
        assert not state["task"].done()
    finally:
        release.set()
        thread.join(timeout=2)

    assert not thread.is_alive()
    assert not failures
