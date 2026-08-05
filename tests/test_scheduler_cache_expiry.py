import threading
from unittest.mock import Mock

from app import scheduler as scheduler_module
from app.scheduler import Scheduler


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


def test_meta_cache_expire_does_not_schedule_bulk_cache_clear(monkeypatch):
    """单条缓存 TTL 不应再被用于注册整批缓存清理任务。"""
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
        "WorkflowChain",
        "PluginManager",
    ]:
        monkeypatch.setattr(scheduler_module, name, lambda: generic_chain)
    monkeypatch.setattr(
        scheduler_module.ServiceConfigHelper,
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
    monkeypatch.setattr(scheduler_module.settings, "COOKIECLOUD_INTERVAL", 0)
    monkeypatch.setattr(scheduler_module.settings, "SUBSCRIBE_SEARCH", False)
    monkeypatch.setattr(scheduler_module.settings, "SUBSCRIBE_MODE", "rss")
    monkeypatch.setattr(scheduler_module.settings, "SUBSCRIBE_RSS_INTERVAL", 30)
    monkeypatch.setattr(scheduler_module.settings, "SITEDATA_REFRESH_INTERVAL", 0)
    monkeypatch.setattr(scheduler_module.settings, "MEMORY_GC_INTERVAL", 0)
    monkeypatch.setattr(scheduler_module.settings, "AI_AGENT_ENABLE", False)
    monkeypatch.setattr(scheduler_module.settings, "DATA_CLEANUP_ENABLE", False)
    monkeypatch.setattr(scheduler_module.settings, "USAGE_STATISTIC_SHARE", False)

    scheduler = object.__new__(Scheduler)
    scheduler._scheduler = None
    scheduler._event = threading.Event()
    scheduler._lock = threading.RLock()
    scheduler._jobs = {}
    scheduler._auth_count = 0
    scheduler._auth_message = False

    scheduler.init()

    scheduled_job_ids = {job["id"] for job in background_scheduler.jobs}
    assert "clear_cache" not in scheduled_job_ids
    assert "clear_cache" in scheduler._jobs
    assert background_scheduler.started is True
