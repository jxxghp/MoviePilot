"""数据库备份与宿主调度器的接入合同。"""

import ast
import threading
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

from app import scheduler as scheduler_module
from app.scheduler import Scheduler
from app.application.configuration import SchedulerRuntimeConfig


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
    scheduler._handles = {}
    scheduler._job_generations = {}
    scheduler._active_job_generations = {}
    scheduler._agent_task_reservations = {}
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
    monkeypatch.setattr(scheduler_module.TimerUtils, "build_schedule_trigger", Mock(return_value=trigger))
    config = _config(db_backup_enable=True, db_backup_cron="0 3 * * *")

    scheduler._register_database_backup_job(config)
    scheduler._register_database_backup_job(config)

    assert list(scheduler._scheduler.jobs) == ["database_backup"]
    assert scheduler._scheduler.jobs["database_backup"]["replace_existing"] is True


def test_scheduled_backup_uses_registered_database_governance(monkeypatch) -> None:
    governance = Mock()
    monkeypatch.setattr(scheduler_module, "get_database_governance", lambda: governance)

    result = Scheduler.database_backup()

    assert result is governance.create_backup.return_value
    governance.create_backup.assert_called_once_with()


def test_scheduler_database_dependencies_are_explicit_module_imports() -> None:
    tree = ast.parse(
        (Path(__file__).parents[1] / "app" / "scheduler.py").read_text(encoding="utf-8")
    )
    function_imports = [
        node
        for function in ast.walk(tree)
        if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef))
        for node in ast.walk(function)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        and getattr(node, "module", "")
        and str(getattr(node, "module", "")).startswith("app.application.database")
    ]
    assert function_imports == []
