import asyncio
import signal
import threading
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI

from app.startup import lifecycle, modules_initializer
from app.adapters.network import http as http_utils


def _assert_completed_once(mock: MagicMock) -> None:
    if isinstance(mock, AsyncMock):
        mock.assert_awaited_once_with()
    else:
        mock.assert_called_once_with()


def _patch_lifespan(monkeypatch, *, failing_step: str | None = None) -> dict:
    """隔离 lifespan 的外部依赖，并按名称注入一个关闭失败"""
    monkeypatch.setattr(lifecycle.settings, "MOVIEPILOT_SAFE_MODE", False)
    monkeypatch.setattr(lifecycle.global_vars, "set_loop", MagicMock())
    monkeypatch.setattr(lifecycle.global_vars, "stop_system", MagicMock())

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
    monkeypatch.setattr(lifecycle, "configure_plugin_services", MagicMock())
    monkeypatch.setattr(lifecycle, "init_modules", AsyncMock())

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
        "stop_workflow": MagicMock(),
        "stop_command": MagicMock(),
        "stop_monitor": MagicMock(),
        "stop_scheduler": MagicMock(),
        "stop_plugins": MagicMock(),
        "stop_modules": AsyncMock(),
        "close_http": AsyncMock(),
    }
    for name in (
        "stop_workflow",
        "stop_command",
        "stop_monitor",
        "stop_scheduler",
        "stop_plugins",
    ):
        monkeypatch.setattr(lifecycle, name, shutdown_steps[name])
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

    logger_shutdown = MagicMock()
    monkeypatch.setattr(lifecycle.LoggerManager, "shutdown", logger_shutdown)
    shutdown_steps["logger"] = logger_shutdown
    return shutdown_steps


@pytest.mark.parametrize(
    "failing_step",
    [
        "backup_plugins",
        "stop_workflow",
        "stop_command",
        "stop_monitor",
        "stop_scheduler",
        "stop_plugins",
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

    lifecycle.global_vars.stop_system.assert_called_once_with()
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


def test_lifespan_waits_for_plugin_settlement_before_shutdown(monkeypatch):
    """关停必须等待插件恢复线程结束，避免与备份和资源释放并发。"""
    shutdown_steps = _patch_lifespan(monkeypatch)
    order = []
    shutdown_steps["backup_plugins"].side_effect = lambda: order.append("backup")

    async def run_lifespan():
        started = asyncio.Event()
        release = asyncio.Event()

        async def settle_plugins():
            started.set()
            await release.wait()
            order.append("settled")

        lifecycle.init_extra.side_effect = settle_plugins
        async with lifecycle.lifespan(FastAPI()):
            await started.wait()
            asyncio.get_running_loop().call_later(0.02, release.set)

    asyncio.run(run_lifespan())

    assert order[:2] == ["settled", "backup"]


def test_lifespan_configures_plugin_services_before_restore(monkeypatch):
    """插件恢复依赖的外部系统服务必须先于恢复阶段完成装配。"""
    shutdown_steps = _patch_lifespan(monkeypatch)
    order = []
    lifecycle.configure_plugin_services.side_effect = lambda: order.append("configure")
    lifecycle.SystemChain.return_value.restore_plugins.side_effect = (
        lambda: order.append("restore")
    )

    async def run_lifespan():
        async with lifecycle.lifespan(FastAPI()):
            pass

    asyncio.run(run_lifespan())

    assert order == ["configure", "restore"]
    _assert_completed_once(shutdown_steps["close_http"])


def test_lifespan_safe_mode_skips_optional_runtime(monkeypatch):
    """安全模式只启动基础模块，并跳过插件及可选后台服务。"""
    shutdown_steps = _patch_lifespan(monkeypatch)
    monkeypatch.setattr(lifecycle.settings, "MOVIEPILOT_SAFE_MODE", True)

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
        "stop_command",
        "stop_monitor",
        "stop_scheduler",
        "stop_plugins",
    ):
        shutdown_steps[name].assert_not_called()
    _assert_completed_once(shutdown_steps["stop_modules"])
    _assert_completed_once(shutdown_steps["close_http"])
    _assert_completed_once(shutdown_steps["logger"])


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
        "数据库准备",
        "HTTP 基础能力",
        "领域依赖装配",
        "数据库引擎预热",
        "数据库连接预算",
        "数据端口装配",
        "路由",
        "模块服务",
        "插件备份恢复",
        "插件",
        "定时器",
        "监控器",
        "待处理整理回放",
        "命令服务",
        "工作流",
    ]
    assert normal_stop == [
        "插件备份",
        "工作流",
        "命令服务",
        "监控器",
        "定时器",
        "插件",
        "模块服务",
        "HTTP 基础能力",
    ]
    assert safe_names == {
        "数据库准备",
        "HTTP 基础能力",
        "领域依赖装配",
        "数据库引擎预热",
        "数据库连接预算",
        "数据端口装配",
        "路由",
        "模块服务",
    }
    assert all(item["start_failure"] == "fail_fast" for item in normal)
    assert all(item["stop_failure"] == "continue" for item in normal)
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
    monkeypatch.setattr(lifecycle, "init_routers", lambda _app: calls.append("init_routers"))
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

    # 失败要发生在任何东西被初始化之前，否则模块起来了却没人关：关停块在 yield 处才开始
    lifecycle.init_routers.assert_not_called()
    lifecycle.init_modules.assert_not_called()


def test_uvicorn_signal_publishes_stop_before_server_exit(monkeypatch):
    """Uvicorn 接管系统信号时必须先发布协作停止标志"""
    from app import main

    calls = []
    monkeypatch.setattr(main.global_vars, "stop_system", lambda: calls.append("stop"))
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
    monkeypatch.setattr(main.global_vars, "STOP_EVENT", stop_event)
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


def test_uvicorn_preserves_stop_requested_before_serve(monkeypatch):
    """Uvicorn 启动不能清除数据库初始化阶段已经发布的停止请求"""
    from app import main

    stop_event = threading.Event()
    monkeypatch.setattr(main.global_vars, "STOP_EVENT", stop_event)
    main.global_vars.stop_system()

    async def serve(_self, sockets=None):
        assert main.global_vars.is_system_stopped

    monkeypatch.setattr(main.uvicorn.Server, "serve", serve)
    server = object.__new__(main.MoviePilotServer)
    asyncio.run(server.serve())


@pytest.mark.parametrize("endpoint_name", ["restart_system", "upgrade_system"])
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
    """重启或升级失败不能发布或撤销停止请求"""
    from app.api.endpoints import system

    stop_event = threading.Event()
    if initially_stopped:
        stop_event.set()
    monkeypatch.setattr(system.global_vars, "STOP_EVENT", stop_event)
    monkeypatch.setattr(system.SystemHelper, "can_restart", MagicMock(return_value=True))
    monkeypatch.setattr(
        system.SystemHelper,
        "restart" if endpoint_name == "restart_system" else "upgrade",
        MagicMock(return_value=(False, "restart failed")),
    )

    if endpoint_name == "restart_system":
        response = system.restart_system(None)
    else:
        response = system.upgrade_system(None, None)

    assert not response.success
    assert stop_event.is_set() is initially_stopped


def test_command_restart_failure_does_not_publish_stop_request(monkeypatch):
    """命令重启失败时进程仍在运行，不能提前发布停止请求"""
    from app.application.orchestration.system import SystemChain
    from app.runtime.config import global_vars

    stop_event = threading.Event()
    monkeypatch.setattr(global_vars, "STOP_EVENT", stop_event)
    monkeypatch.setattr(SystemChain, "backup_plugins", MagicMock())
    restart = MagicMock(return_value=(False, "restart failed"))
    monkeypatch.setattr("app.application.orchestration.system.SystemHelper.restart", restart)

    chain = object.__new__(SystemChain)
    chain.restart(channel=None, userid=None)

    restart.assert_called_once_with()
    assert not stop_event.is_set()


def test_stop_modules_continues_after_internal_owner_failures(monkeypatch):
    """模块关闭编排中的多个失败不能阻断其余清理"""
    stop_agent = AsyncMock(side_effect=RuntimeError("agent failed"))
    monkeypatch.setattr(modules_initializer, "stop_agent", stop_agent)
    dependencies = _patch_module_shutdown_dependencies(monkeypatch)
    dependencies["module"].side_effect = RuntimeError("module failed")

    asyncio.run(modules_initializer.stop_modules())

    stop_agent.assert_awaited_once_with()
    for dependency in dependencies.values():
        _assert_completed_once(dependency)


def _patch_module_shutdown_dependencies(monkeypatch) -> dict:
    """替换 stop_modules 的资源所有者，避免测试启动真实后台服务"""
    dependencies = {}
    for name, method_name in (
        ("ModuleManager", "shutdown"),
        ("EventManager", "stop"),
        ("DohHelper", "shutdown"),
        ("ThreadHelper", "shutdown"),
        ("RedisHelper", "close"),
    ):
        instance = MagicMock()
        setattr(instance, method_name, MagicMock())
        monkeypatch.setattr(
            modules_initializer,
            name,
            MagicMock(return_value=instance),
        )
        key = name.removesuffix("Helper").removesuffix("Manager").lower()
        dependencies[key] = getattr(instance, method_name)

    for name in (
        "close_browser_sessions",
        "stop_message",
        "stop_frontend",
        "clear_temp",
    ):
        dependency = MagicMock()
        monkeypatch.setattr(modules_initializer, name, dependency)
        dependencies[name] = dependency

    stop_managed_resources = AsyncMock()
    monkeypatch.setattr(
        modules_initializer,
        "stop_managed_resources",
        stop_managed_resources,
    )
    dependencies["stop_managed_resources"] = stop_managed_resources

    async_redis = MagicMock()
    async_redis.close = AsyncMock()
    monkeypatch.setattr(
        modules_initializer,
        "AsyncRedisHelper",
        MagicMock(return_value=async_redis),
    )
    dependencies["async_redis"] = async_redis.close
    close_database = AsyncMock()
    monkeypatch.setattr(modules_initializer, "close_database", close_database)
    dependencies["close_database"] = close_database
    return dependencies


def test_browser_sessions_close_before_managed_resources(monkeypatch) -> None:
    """显示等宿主资源必须晚于浏览器会话释放，避免存活上下文失去依赖。"""
    calls: list[str] = []
    monkeypatch.setattr(modules_initializer, "stop_agent", AsyncMock())
    dependencies = _patch_module_shutdown_dependencies(monkeypatch)
    dependencies["close_browser_sessions"].side_effect = lambda: calls.append("browser")

    async def stop_resources() -> None:
        calls.append("resources")

    dependencies["stop_managed_resources"].side_effect = stop_resources

    asyncio.run(modules_initializer.stop_modules())

    assert calls == ["browser", "resources"]


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
