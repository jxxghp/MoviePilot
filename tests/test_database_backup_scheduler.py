"""数据库备份与宿主调度器的接入合同。"""

import ast
from pathlib import Path
from unittest.mock import Mock

from app import scheduler as scheduler_module
from app.scheduler import Scheduler


class _SchedulerStub:
    def __init__(self) -> None:
        self.jobs = {}

    def add_job(self, func, *, trigger, id, **kwargs) -> None:
        self.jobs[id] = {"func": func, "trigger": trigger, **kwargs}


def _scheduler() -> Scheduler:
    scheduler = object.__new__(Scheduler)
    scheduler._scheduler = _SchedulerStub()
    scheduler._jobs = {}
    return scheduler


def test_database_backup_schedule_only_watches_job_shape() -> None:
    assert Scheduler.CONFIG_WATCH.intersection({
        "DB_BACKUP_ENABLE",
        "DB_BACKUP_CRON",
        "DB_BACKUP_PATH",
        "DB_BACKUP_RETENTION_DAYS",
        "DB_BACKUP_MAX_COUNT",
    }) == {"DB_BACKUP_ENABLE", "DB_BACKUP_CRON"}


def test_disabled_database_backup_does_not_register_job(monkeypatch) -> None:
    scheduler = _scheduler()
    monkeypatch.setattr(scheduler_module.settings, "DB_BACKUP_ENABLE", False)

    scheduler._register_database_backup_job()

    assert scheduler._scheduler.jobs == {}


def test_enabled_database_backup_without_cron_does_not_register_job(monkeypatch) -> None:
    """总开关开启但未配置周期时，不启用定时备份。"""
    scheduler = _scheduler()
    monkeypatch.setattr(scheduler_module.settings, "DB_BACKUP_ENABLE", True)
    monkeypatch.setattr(scheduler_module.settings, "DB_BACKUP_CRON", "")

    scheduler._register_database_backup_job()

    assert scheduler._scheduler.jobs == {}


def test_enabled_database_backup_registers_single_replaceable_job(monkeypatch) -> None:
    scheduler = _scheduler()
    trigger = object()
    monkeypatch.setattr(scheduler_module.settings, "DB_BACKUP_ENABLE", True)
    monkeypatch.setattr(scheduler_module.settings, "DB_BACKUP_CRON", "0 3 * * *")
    monkeypatch.setattr(scheduler_module.TimerUtils, "build_schedule_trigger", Mock(return_value=trigger))

    scheduler._register_database_backup_job()
    scheduler._register_database_backup_job()

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
