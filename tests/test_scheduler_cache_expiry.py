import asyncio
import threading
from unittest.mock import AsyncMock, Mock

import pytest

from app.application.configuration import SchedulerRuntimeConfig
from app.scheduler import catalog as scheduler_catalog
from app.scheduler import lifecycle as scheduler_lifecycle
from app.scheduler import reconcile as scheduler_reconcile
from app.scheduler.facade import Scheduler
from app.scheduler.registry import ExecutionRegistry
from app.startup.initializers import scheduler as scheduler_initializer


class _BackgroundSchedulerStub:
    """记录系统定时任务注册结果的调度器替身。"""

    def __init__(self):
        """初始化任务记录。"""
        self.jobs = []
        self.started = False

    def add_job(self, func, trigger, **kwargs):
        """记录一次任务注册。"""
        self.jobs.append({"func": func, "trigger": trigger, **kwargs})

    def start(self):
        """记录调度器已启动。"""
        self.started = True


def test_scheduler_constructor_does_not_start_background_jobs(monkeypatch):
    """取得调度器单例不应绕过应用生命周期启动后台任务。"""
    init = Mock()
    monkeypatch.setattr(Scheduler, "init", init)

    scheduler = object.__new__(Scheduler)
    scheduler.__init__()

    init.assert_not_called()
    assert scheduler._scheduler is None


def test_scheduler_initializer_starts_background_jobs(monkeypatch):
    """应用启动入口负责显式启动已经构造的调度器。"""
    scheduler = Mock()
    monkeypatch.setattr(scheduler_initializer, "Scheduler", Mock(return_value=scheduler))

    scheduler_initializer.init_scheduler()

    scheduler.init.assert_called_once_with()


def test_scheduler_initializer_stop_preserves_sync_abi(monkeypatch):
    """同步调用停止入口仍应立即关闭 Scheduler，不返回 coroutine。"""
    scheduler = Mock()
    monkeypatch.setattr(scheduler_initializer, "Scheduler", Mock(return_value=scheduler))

    assert scheduler_initializer.stop_scheduler() is None
    scheduler.stop.assert_called_once_with()
    scheduler.stop_async.assert_not_called()


def test_scheduler_initializer_stop_awaits_in_running_loop(monkeypatch):
    """生命周期事件循环中的停止入口应返回可等待的异步收口。"""
    scheduler = Mock()
    scheduler.stop_async = AsyncMock()
    monkeypatch.setattr(scheduler_initializer, "Scheduler", Mock(return_value=scheduler))

    async def scenario():
        result = scheduler_initializer.stop_scheduler()
        assert result is not None
        await result

    asyncio.run(scenario())
    scheduler.stop_async.assert_awaited_once_with()
    scheduler.stop.assert_not_called()


def test_scheduler_initializer_retains_bindings_when_async_stop_fails(monkeypatch):
    """Scheduler 未停止时不得撤销仓储、业务能力或 concrete class 登记。"""
    scheduler = Mock()
    scheduler.stop_async = AsyncMock(side_effect=RuntimeError("scheduler busy"))
    reset_bindings = Mock()
    monkeypatch.setattr(scheduler_initializer, "Scheduler", Mock(return_value=scheduler))
    monkeypatch.setattr(
        scheduler_initializer,
        "reset_scheduler_bindings",
        reset_bindings,
    )

    async def scenario() -> None:
        """等待异步关闭失败并验证 reset 未被调用。"""
        result = scheduler_initializer.stop_scheduler()
        assert result is not None
        with pytest.raises(RuntimeError, match="scheduler busy"):
            await result

    asyncio.run(scenario())
    reset_bindings.assert_not_called()


def test_clear_cache_is_manual_only(monkeypatch):
    """缓存清理任务应仅手动执行，不注册到调度器自动运行。"""
    background_scheduler = _BackgroundSchedulerStub()
    services = Mock()
    monkeypatch.setattr(scheduler_catalog, "get_plugin_manager", lambda: Mock())
    monkeypatch.setattr(
        scheduler_catalog,
        "get_mediaserver_configs",
        lambda **_kwargs: [],
    )
    monkeypatch.setattr(
        scheduler_catalog,
        "BackgroundScheduler",
        lambda **kwargs: background_scheduler,
    )
    monkeypatch.setattr(Scheduler, "stop", lambda self: None)
    monkeypatch.setattr(Scheduler, "init_workflow_jobs", lambda self: None)
    monkeypatch.setattr(Scheduler, "init_agent_task_jobs", lambda self: None)
    monkeypatch.setattr(Scheduler, "init_plugin_jobs", lambda self: None)
    monkeypatch.setattr(
        scheduler_lifecycle,
        "get_scheduler_runtime_config",
        lambda: SchedulerRuntimeConfig(
            False, "Asia/Shanghai", 1, False, "", 0, None, False, 24,
            "rss", 30, False, 0, 0, False, None, False, None,
        ),
    )

    scheduler = object.__new__(Scheduler)
    scheduler._scheduler = None
    scheduler._event = threading.Event()
    scheduler._lock = threading.RLock()
    scheduler._jobs = {}
    scheduler._lifecycle_state = "new"
    scheduler._registry = ExecutionRegistry(scheduler._lock)
    scheduler._agent_task_interruptions_reconciled = True
    scheduler._auth_count = 0
    scheduler._auth_message = False
    scheduler._services = services

    scheduler.init()

    scheduled_job_ids = {job["id"] for job in background_scheduler.jobs}
    assert "clear_cache" not in scheduled_job_ids
    assert "clear_cache" in scheduler._jobs
    assert scheduler._jobs["clear_cache"]["manual"] is True
    assert scheduler._jobs["outbox_dispatch"]["name"] == "重试未完成的后台处理"
    outbox_job = next(job for job in background_scheduler.jobs if job["id"] == "outbox_dispatch")
    assert outbox_job["name"] == "重试未完成的后台处理"
    assert background_scheduler.started is True


def test_user_auth_refreshes_plugin_routes_after_runtime_reinitialization(monkeypatch):
    """自动认证重建插件实例后必须同步刷新动态路由投影。"""
    scheduler = object.__new__(Scheduler)
    scheduler._auth_count = 1
    scheduler._auth_message = False
    scheduler._auth_plugin_routes_pending = False
    plugin_manager = Mock()
    plugin_jobs = Mock()
    refresh_routes = Mock()
    message_chain = Mock()
    monkeypatch.setattr(
        scheduler_reconcile,
        "get_scheduler_runtime_config",
        lambda: Mock(site_link="https://example.invalid"),
    )
    monkeypatch.setattr(
        scheduler_reconcile,
        "SitesHelper",
        lambda: Mock(auth_level=0, check_user=Mock(return_value=(True, "demo"))),
    )
    monkeypatch.setattr(
        scheduler_reconcile,
        "get_configured_system_config",
        lambda: Mock(get=Mock(return_value=None)),
    )
    scheduler._services = Mock(post_message=message_chain.post_message)
    monkeypatch.setattr(
        scheduler_reconcile,
        "get_plugin_manager",
        lambda: plugin_manager,
    )
    monkeypatch.setattr(scheduler, "init_plugin_jobs", plugin_jobs)
    monkeypatch.setattr(scheduler_reconcile, "register_plugin_api", refresh_routes)

    scheduler.user_auth()

    plugin_manager.init_config.assert_called_once_with()
    plugin_jobs.assert_called_once_with()
    refresh_routes.assert_called_once_with()
    assert scheduler._auth_plugin_routes_pending is False


def test_user_auth_retries_pending_plugin_route_projection(monkeypatch):
    """认证已成功但路由投影失败时，后续认证任务只重试未完成的投影。"""
    scheduler = object.__new__(Scheduler)
    scheduler._auth_count = 1
    scheduler._auth_message = False
    scheduler._auth_plugin_routes_pending = False
    plugin_manager = Mock()
    plugin_jobs = Mock()
    refresh_routes = Mock(side_effect=[RuntimeError("loop unavailable"), None])
    message_chain = Mock()
    sites = Mock(auth_level=0, check_user=Mock(return_value=(True, "demo")))
    monkeypatch.setattr(
        scheduler_reconcile,
        "get_scheduler_runtime_config",
        lambda: Mock(site_link="https://example.invalid"),
    )
    monkeypatch.setattr(scheduler_reconcile, "SitesHelper", lambda: sites)
    monkeypatch.setattr(
        scheduler_reconcile,
        "get_configured_system_config",
        lambda: Mock(get=Mock(return_value=None)),
    )
    scheduler._services = Mock(post_message=message_chain.post_message)
    monkeypatch.setattr(
        scheduler_reconcile,
        "get_plugin_manager",
        lambda: plugin_manager,
    )
    monkeypatch.setattr(scheduler, "init_plugin_jobs", plugin_jobs)
    monkeypatch.setattr(scheduler_reconcile, "register_plugin_api", refresh_routes)

    with pytest.raises(RuntimeError, match="loop unavailable"):
        scheduler.user_auth()

    assert scheduler._auth_plugin_routes_pending is True
    sites.auth_level = 2
    scheduler.user_auth()

    assert scheduler._auth_plugin_routes_pending is False
    assert refresh_routes.call_count == 2
    plugin_manager.init_config.assert_called_once_with()
    plugin_jobs.assert_called_once_with()
    sites.check_user.assert_called_once_with()
