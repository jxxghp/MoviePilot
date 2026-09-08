"""数据库备份与宿主调度器的接入合同。"""

import ast
import threading
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest

from app.application.configuration import SchedulerRuntimeConfig
from app.scheduler import catalog as scheduler_catalog
from app.scheduler import maintenance as scheduler_maintenance
from app.scheduler.facade import Scheduler
from app.scheduler.registry import ExecutionRegistry


class _SchedulerStub:
    """记录 Scheduler 注册结果的最小调度器替身。"""

    def __init__(self) -> None:
        """初始化空作业表。"""
        self.jobs = {}

    def add_job(self, func, *, trigger, id, **kwargs) -> None:
        """按作业 ID 保存最近一次注册参数。"""
        self.jobs[id] = {"func": func, "trigger": trigger, **kwargs}


def _scheduler() -> Scheduler:
    """构造不启动后台线程的 Scheduler。"""
    scheduler = object.__new__(Scheduler)
    scheduler._scheduler = _SchedulerStub()
    scheduler._jobs = {}
    scheduler._lock = threading.RLock()
    scheduler._lifecycle_state = "running"
    scheduler._registry = ExecutionRegistry(scheduler._lock)
    return scheduler


def _config(**changes) -> SchedulerRuntimeConfig:
    """构造数据库备份测试所需的最小调度配置快照。"""
    config = SchedulerRuntimeConfig(
        dev=False,
        timezone="Asia/Shanghai",
        scheduler_workers=1,
        db_backup_enable=False,
        db_backup_cron="",
        cookiecloud_interval=None,
        mediaserver_sync_interval=None,
        subscribe_search=False,
        subscribe_search_interval=24,
        subscribe_mode="rss",
        subscribe_rss_interval=30,
        data_cleanup_enable=False,
        sitedata_refresh_interval=None,
        memory_gc_interval=None,
        ai_agent_enable=False,
        ai_agent_job_interval=None,
        usage_statistic_share=False,
        site_link=None,
        auto_update=False,
        auto_update_resource=False,
    )
    return replace(config, **changes)


def test_database_backup_schedule_only_watches_job_shape() -> None:
    assert Scheduler.CONFIG_WATCH.intersection({
        "DB_BACKUP_ENABLE",
        "DB_BACKUP_CRON",
        "DB_BACKUP_ON_UPGRADE",
        "DB_BACKUP_PATH",
        "DB_BACKUP_RETENTION_DAYS",
        "DB_BACKUP_MAX_COUNT",
    }) == {"DB_BACKUP_ENABLE", "DB_BACKUP_CRON"}


def test_auto_update_setting_is_hot_reloadable() -> None:
    """主程序或资源开关变更时均应触发 Scheduler 重建。"""
    assert "MOVIEPILOT_AUTO_UPDATE" in Scheduler.CONFIG_WATCH
    assert "AUTO_UPDATE_RESOURCE" in Scheduler.CONFIG_WATCH


def test_disabled_database_backup_does_not_register_job() -> None:
    """关闭备份时不注册作业。"""
    scheduler = _scheduler()

    scheduler._register_database_backup_job(_config())

    assert scheduler._scheduler.jobs == {}


def test_enabled_database_backup_without_cron_does_not_register_job() -> None:
    """总开关开启但未配置周期时，不启用定时备份。"""
    scheduler = _scheduler()
    scheduler._register_database_backup_job(_config(db_backup_enable=True))

    assert scheduler._scheduler.jobs == {}


def test_enabled_database_backup_registers_single_replaceable_job(monkeypatch) -> None:
    scheduler = _scheduler()
    trigger = object()
    monkeypatch.setattr(scheduler_catalog.TimerUtils, "build_schedule_trigger", Mock(return_value=trigger))
    config = _config(db_backup_enable=True, db_backup_cron="0 3 * * *")

    scheduler._register_database_backup_job(config)
    scheduler._register_database_backup_job(config)

    assert list(scheduler._scheduler.jobs) == ["database_backup"]
    assert scheduler._scheduler.jobs["database_backup"]["replace_existing"] is True


@pytest.mark.parametrize("auto_update", [False, True])
@pytest.mark.parametrize("auto_update_resource", [False, True])
def test_auto_update_check_is_registered_only_when_enabled(
    monkeypatch, auto_update, auto_update_resource
) -> None:
    """任一开关开启即注册检查任务，均关闭则不注册。"""
    scheduler = _scheduler()
    scheduler._services = Mock()
    background_scheduler = Mock()
    monkeypatch.setattr(scheduler_catalog, "BackgroundScheduler", lambda **_kwargs: background_scheduler)
    monkeypatch.setattr(scheduler_catalog, "get_plugin_manager", lambda: Mock())
    monkeypatch.setattr(scheduler_catalog, "get_mediaserver_configs", lambda **_kwargs: [])
    monkeypatch.setattr(scheduler, "init_workflow_jobs", lambda: None)
    monkeypatch.setattr(scheduler, "init_agent_task_jobs", lambda: None)
    monkeypatch.setattr(scheduler, "init_plugin_jobs", lambda: None)

    scheduler_catalog.SchedulerCatalogOwner._initialize_catalog(
        scheduler, _config(auto_update=auto_update, auto_update_resource=auto_update_resource)
    )
    assert any(
        call.kwargs.get("id") == "system_update_check"
        for call in background_scheduler.add_job.call_args_list
    ) is (auto_update or auto_update_resource)
    assert ("system_update_check" in scheduler._jobs) is (auto_update or auto_update_resource)


def test_scheduled_backup_uses_registered_database_governance(monkeypatch) -> None:
    governance = Mock()
    monkeypatch.setattr(scheduler_maintenance, "get_database_governance", lambda: governance)

    result = Scheduler.database_backup()

    assert result is governance.create_backup.return_value
    governance.create_backup.assert_called_once_with()


@pytest.mark.parametrize("enabled", [False, True])
def test_subscription_search_scans_due_items_only_when_enabled(monkeypatch, enabled) -> None:
    """系统开关控制到期扫描，五分钟扫描节奏与实际搜索间隔分别传递。"""
    scheduler = _scheduler()
    scheduler._services = Mock()
    background_scheduler = Mock()
    monkeypatch.setattr(scheduler_catalog, "BackgroundScheduler", lambda **_kwargs: background_scheduler)
    monkeypatch.setattr(scheduler_catalog, "get_plugin_manager", lambda: Mock())
    monkeypatch.setattr(scheduler_catalog, "get_mediaserver_configs", lambda **_kwargs: [])
    monkeypatch.setattr(scheduler, "init_workflow_jobs", lambda: None)
    monkeypatch.setattr(scheduler, "init_agent_task_jobs", lambda: None)
    monkeypatch.setattr(scheduler, "init_plugin_jobs", lambda: None)

    scheduler._initialize_catalog(_config(subscribe_search=enabled, subscribe_search_interval=48))

    calls = [call for call in background_scheduler.add_job.call_args_list
             if call.kwargs.get("id") == "subscribe_search"]
    assert bool(calls) is enabled
    if enabled:
        assert calls[0].kwargs["minutes"] == 5
        assert scheduler._jobs["subscribe_search"]["kwargs"]["scheduled_interval"] == 48


def test_scheduler_database_dependencies_are_explicit_module_imports() -> None:
    scheduler_root = Path(__file__).parents[1] / "app" / "scheduler"
    trees = [ast.parse(path.read_text(encoding="utf-8")) for path in scheduler_root.glob("*.py")]
    function_imports = [
        node
        for tree in trees
        for function in ast.walk(tree)
        if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef))
        for node in ast.walk(function)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        and getattr(node, "module", "")
        and str(getattr(node, "module", "")).startswith("app.application.database")
    ]
    assert function_imports == []
