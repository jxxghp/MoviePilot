"""宿主业务定时作业清单的声明契约测试。

清单是数据：作业的登记项、触发条件与仅手动执行的语义都由清单本身表达，
调度器组合根只负责把声明登记到执行引擎。
"""

import ast
import threading
from pathlib import Path
from unittest.mock import Mock

import pytest

from app.scheduler import Scheduler
from app.startup.bindings.scheduling import manifest as manifest_module
from app.startup.bindings.scheduling.systemjobs import UserAuthChecker

# 无论开关如何都必须出现在运行状态登记中的作业
ALWAYS_REGISTERED = {
    "cookiecloud",
    "mediaserver_sync",
    "subscribe_tmdb",
    "subscribe_search",
    "new_subscribe_search",
    "subscribe_refresh",
    "subscribe_follow",
    "transfer",
    "clear_cache",
    "data_cleanup",
    "user_auth",
    "scheduler_job",
    "random_wallpager",
    "sitedata_refresh",
    "recommend_refresh",
    "plugin_market_refresh",
    "subscribe_calendar_cache",
    "full_gc",
    "agent_heartbeat",
    "usage_report",
}


class _SchedulerStub:
    """按注册顺序记录调度器任务登记的替身。"""

    def __init__(self) -> None:
        """初始化任务登记表。"""
        self.jobs = {}

    def add_job(self, func, *, trigger, id, **kwargs) -> None:
        """记录一次任务登记。"""
        self.jobs[id] = {"func": func, "trigger": trigger, **kwargs}

    def start(self) -> None:
        """记录调度器已启动。"""


@pytest.fixture(name="stub_business_domains")
def fixture_stub_business_domains(monkeypatch):
    """把清单依赖的业务域全部替换为替身，只保留声明结构。"""
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
        monkeypatch.setattr(manifest_module, name, Mock())
    monkeypatch.setattr(
        manifest_module.ServiceConfigHelper,
        "get_mediaserver_configs",
        lambda: [],
    )
    monkeypatch.setattr(manifest_module.settings, "SUBSCRIBE_MODE", "rss")
    monkeypatch.setattr(manifest_module.settings, "SUBSCRIBE_RSS_INTERVAL", 30)
    monkeypatch.setattr(manifest_module.settings, "DB_BACKUP_ENABLE", False)


def _build_jobs():
    """按当前配置构建宿主作业清单。"""
    return manifest_module.build_host_jobs(user_auth=lambda: None)


def test_manifest_registers_every_business_job_regardless_of_switches(
        monkeypatch, stub_business_domains
):
    """作业开关只决定是否注册触发，不影响运行状态登记项本身。"""
    for name in [
        "SUBSCRIBE_SEARCH",
        "DATA_CLEANUP_ENABLE",
        "USAGE_STATISTIC_SHARE",
        "AI_AGENT_ENABLE",
    ]:
        monkeypatch.setattr(manifest_module.settings, name, False)
    monkeypatch.setattr(manifest_module.settings, "COOKIECLOUD_INTERVAL", 0)
    monkeypatch.setattr(manifest_module.settings, "SITEDATA_REFRESH_INTERVAL", 0)
    monkeypatch.setattr(manifest_module.settings, "MEMORY_GC_INTERVAL", 0)

    jobs = {job.id: job for job in _build_jobs()}

    assert ALWAYS_REGISTERED <= set(jobs)
    assert jobs["subscribe_search"].triggers == ()
    assert jobs["data_cleanup"].triggers == ()
    assert jobs["usage_report"].triggers == ()
    assert jobs["agent_heartbeat"].triggers == ()
    assert jobs["cookiecloud"].triggers == ()
    assert jobs["sitedata_refresh"].triggers == ()
    assert jobs["full_gc"].triggers == ()
    # 媒体服务器总任务始终登记但从不自动触发
    assert jobs["mediaserver_sync"].triggers == ()


def test_manifest_marks_cache_clear_manual_only(stub_business_domains):
    """缓存清理只登记运行状态，不注册任何自动触发。"""
    jobs = {job.id: job for job in _build_jobs()}

    assert jobs["clear_cache"].manual is True
    assert jobs["clear_cache"].triggers == ()


def test_manifest_expands_spider_mode_into_distinct_trigger_ids(
        monkeypatch, stub_business_domains
):
    """站点首页刷新模式下，一条作业展开为多条互不覆盖的触发登记。"""
    monkeypatch.setattr(manifest_module.settings, "SUBSCRIBE_MODE", "spider")

    jobs = {job.id: job for job in _build_jobs()}
    triggers = jobs["subscribe_refresh"].triggers

    assert len(triggers) > 1
    assert len({trigger.suffix for trigger in triggers}) == len(triggers)
    assert all(trigger.trigger == "cron" for trigger in triggers)


def test_manifest_clamps_rss_interval(monkeypatch, stub_business_domains):
    """越界的 RSS 刷新周期在构建清单时收敛回合法范围。"""
    monkeypatch.setattr(manifest_module.settings, "SUBSCRIBE_RSS_INTERVAL", 1)
    jobs = {job.id: job for job in _build_jobs()}
    assert jobs["subscribe_refresh"].triggers[0].options["minutes"] == 5

    monkeypatch.setattr(manifest_module.settings, "SUBSCRIBE_RSS_INTERVAL", "")
    jobs = {job.id: job for job in _build_jobs()}
    assert jobs["subscribe_refresh"].triggers[0].options["minutes"] == 30


def test_scheduler_registers_manifest_jobs_into_engine(stub_business_domains):
    """组合根把作业声明逐条登记为运行状态与调度器任务。"""
    scheduler = object.__new__(Scheduler)
    scheduler._lock = threading.RLock()
    scheduler._jobs = {}
    scheduler._scheduler = _SchedulerStub()

    scheduler._register_job(
        manifest_module.ScheduledJob(
            id="demo",
            name="演示作业",
            func=lambda: None,
            kwargs={"state": "R"},
            provider_name="[系统]",
            triggers=(
                manifest_module.ScheduledTrigger(
                    trigger="interval",
                    options={"minutes": 5},
                ),
                manifest_module.ScheduledTrigger(
                    trigger="cron",
                    options={"hour": 3},
                    suffix="|3:0",
                    name="演示作业-凌晨",
                    replace_existing=True,
                ),
            ),
        )
    )

    assert scheduler._jobs["demo"] == {
        "name": "演示作业",
        "func": scheduler._jobs["demo"]["func"],
        "running": False,
        "kwargs": {"state": "R"},
        "provider_name": "[系统]",
    }
    assert set(scheduler._scheduler.jobs) == {"demo", "demo|3:0"}
    assert scheduler._scheduler.jobs["demo"]["kwargs"] == {"job_id": "demo"}
    assert scheduler._scheduler.jobs["demo"]["minutes"] == 5
    assert "replace_existing" not in scheduler._scheduler.jobs["demo"]
    assert scheduler._scheduler.jobs["demo|3:0"]["name"] == "演示作业-凌晨"
    assert scheduler._scheduler.jobs["demo|3:0"]["replace_existing"] is True
    # 展开出的多条触发共用同一份运行状态登记
    assert scheduler._scheduler.jobs["demo|3:0"]["kwargs"] == {"job_id": "demo"}


def test_user_auth_counter_survives_config_reload(stub_business_domains):
    """认证失败计数与调度器实例同生命周期，重建作业清单不得清零。"""
    checker = UserAuthChecker(on_authenticated=lambda: None)
    checker._auth_count = 7

    first = {job.id: job for job in manifest_module.build_host_jobs(user_auth=checker.check)}
    second = {job.id: job for job in manifest_module.build_host_jobs(user_auth=checker.check)}

    assert first["user_auth"].func.__self__ is checker
    assert second["user_auth"].func.__self__ is checker
    assert checker._auth_count == 7


def test_scheduler_init_does_not_rebuild_the_auth_checker():
    """认证检查作业只在调度器构造期创建，init 重入不得新建实例。"""
    source = ast.parse(
        (Path(__file__).parents[1] / "app" / "scheduler" / "composition.py").read_text(
            encoding="utf-8"
        )
    )
    constructing_methods = {
        method.name
        for cls in ast.walk(source)
        if isinstance(cls, ast.ClassDef)
        for method in cls.body
        if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef))
        for node in ast.walk(method)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "UserAuthChecker"
    }

    assert constructing_methods == {"__init__"}
