import asyncio
import signal
import threading
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI

from app.startup import lifecycle, modules_initializer
from app.utils import http as http_utils


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
        "init_modules",
        "init_plugins",
        "init_scheduler",
        "init_monitor",
        "init_command",
        "init_workflow",
    ):
        monkeypatch.setattr(lifecycle, name, MagicMock())

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
    for step in shutdown_steps.values():
        _assert_completed_once(step)


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
    monkeypatch.setattr(main, "init_db", lambda: calls.append("init_db"))
    monkeypatch.setattr(main, "update_db", lambda: calls.append("update_db"))
    monkeypatch.setattr(main.Server, "run", lambda: calls.append("server"))

    main.run_application()

    assert stop_event.is_set()
    assert calls == [
        "signal",
        "signal",
        "tray",
        "init_db",
        "update_db",
        "server",
    ]


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
    from app.chain.system import SystemChain
    from app.core.config import global_vars

    stop_event = threading.Event()
    monkeypatch.setattr(global_vars, "STOP_EVENT", stop_event)
    monkeypatch.setattr(SystemChain, "backup_plugins", MagicMock())
    restart = MagicMock(return_value=(False, "restart failed"))
    monkeypatch.setattr("app.chain.system.SystemHelper.restart", restart)

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
        ("ModuleManager", "stop"),
        ("EventManager", "stop"),
        ("DisplayHelper", "stop"),
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

    for name in ("stop_message", "stop_frontend", "clear_temp"):
        dependency = MagicMock()
        monkeypatch.setattr(modules_initializer, name, dependency)
        dependencies[name] = dependency

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
    monkeypatch.setattr(http_utils.httpx, "AsyncHTTPTransport", FakeTransport)
    debug = MagicMock()
    monkeypatch.setattr(http_utils.logger, "debug", debug)

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

    debug.assert_any_call(
        "LRU 淘汰共享 transport 时关闭失败: "
        "RuntimeError('eviction close failed')"
    )


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
