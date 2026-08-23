import asyncio
import threading
from unittest.mock import AsyncMock, Mock

from app.scheduler import composition as scheduler_module
from app.scheduler import Scheduler
from app.startup import scheduler_initializer
from app.scheduler import workflows as workflows_module
from app.startup.bindings.scheduling import manifest as manifest_module
from app.startup.bindings.scheduling.systemjobs import UserAuthChecker


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


def test_clear_cache_is_manual_only(monkeypatch):
    """缓存清理任务应仅手动执行，不注册到调度器自动运行。"""
    background_scheduler = _BackgroundSchedulerStub()
    generic_chain = Mock()
    for name in [
        "MediaServerChain",
        "RecommendChain",
        "SchedulerChain",
        "SiteChain",
        "SubscribeChain",
        "TransferChain",
        "WallpaperHelper",
        "PluginManager",
    ]:
        monkeypatch.setattr(manifest_module, name, lambda: generic_chain)
    monkeypatch.setattr(workflows_module, "WorkflowChain", lambda: generic_chain)
    monkeypatch.setattr(
        manifest_module.ServiceConfigHelper,
        "get_mediaserver_configs",
        lambda: [],
    )
    monkeypatch.setattr(
        scheduler_module,
        "BackgroundScheduler",
        lambda **kwargs: background_scheduler,
    )
    monkeypatch.setattr(Scheduler, "stop", lambda self: None)
    monkeypatch.setattr(Scheduler, "init_workflow_jobs", lambda self: None)
    monkeypatch.setattr(Scheduler, "init_agent_task_jobs", lambda self: None)
    monkeypatch.setattr(Scheduler, "init_plugin_jobs", lambda self: None)
    monkeypatch.setattr(scheduler_module.settings, "DEV", False)
    monkeypatch.setattr(manifest_module.settings, "COOKIECLOUD_INTERVAL", 0)
    monkeypatch.setattr(manifest_module.settings, "SUBSCRIBE_SEARCH", False)
    monkeypatch.setattr(manifest_module.settings, "SUBSCRIBE_MODE", "rss")
    monkeypatch.setattr(manifest_module.settings, "SUBSCRIBE_RSS_INTERVAL", 30)
    monkeypatch.setattr(manifest_module.settings, "SITEDATA_REFRESH_INTERVAL", 0)
    monkeypatch.setattr(manifest_module.settings, "MEMORY_GC_INTERVAL", 0)
    monkeypatch.setattr(manifest_module.settings, "AI_AGENT_ENABLE", False)
    monkeypatch.setattr(manifest_module.settings, "DATA_CLEANUP_ENABLE", False)
    monkeypatch.setattr(manifest_module.settings, "USAGE_STATISTIC_SHARE", False)
    monkeypatch.setattr(manifest_module.settings, "DB_BACKUP_ENABLE", False)

    scheduler = object.__new__(Scheduler)
    scheduler._scheduler = None
    scheduler._event = threading.Event()
    scheduler._lock = threading.RLock()
    scheduler._jobs = {}
    scheduler._lifecycle_state = "new"
    scheduler._handles = {}
    scheduler._job_generations = {}
    scheduler._active_job_generations = {}
    scheduler._agent_task_reservations = {}
    scheduler._agent_task_interruptions_reconciled = True
    scheduler._user_auth = UserAuthChecker(on_authenticated=lambda: None)

    scheduler.init()

    scheduled_job_ids = {job["id"] for job in background_scheduler.jobs}
    assert "clear_cache" not in scheduled_job_ids
    assert "clear_cache" in scheduler._jobs
    assert scheduler._jobs["clear_cache"]["manual"] is True
    assert background_scheduler.started is True
