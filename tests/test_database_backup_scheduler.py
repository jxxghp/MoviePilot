"""数据库备份与宿主调度器的接入合同。"""

import ast
from pathlib import Path
from unittest.mock import Mock

from app.scheduler import Scheduler
from app.scheduler import composition as composition_module
from app.startup.bindings.scheduling import manifest as manifest_module
from app.startup.bindings.scheduling import systemjobs as systemjobs_module


def _backup_jobs(monkeypatch) -> list:
    """构建宿主作业清单并取出数据库备份作业。"""
    monkeypatch.setattr(
        manifest_module.ServiceConfigHelper,
        "get_mediaserver_configs",
        lambda: [],
    )
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
    jobs = manifest_module.build_host_jobs(user_auth=lambda: None)
    return [job for job in jobs if job.id == "database_backup"]


def test_database_backup_schedule_only_watches_job_shape() -> None:
    assert Scheduler.CONFIG_WATCH.intersection({
        "DB_BACKUP_ENABLE",
        "DB_BACKUP_CRON",
        "DB_BACKUP_ON_UPGRADE",
        "DB_BACKUP_PATH",
        "DB_BACKUP_RETENTION_DAYS",
        "DB_BACKUP_MAX_COUNT",
    }) == {"DB_BACKUP_ENABLE", "DB_BACKUP_CRON"}


def test_disabled_database_backup_does_not_register_job(monkeypatch) -> None:
    monkeypatch.setattr(manifest_module.settings, "DB_BACKUP_ENABLE", False)

    assert _backup_jobs(monkeypatch) == []


def test_enabled_database_backup_without_cron_does_not_register_job(monkeypatch) -> None:
    """总开关开启但未配置周期时，不启用定时备份。"""
    monkeypatch.setattr(manifest_module.settings, "DB_BACKUP_ENABLE", True)
    monkeypatch.setattr(manifest_module.settings, "DB_BACKUP_CRON", "")

    assert _backup_jobs(monkeypatch) == []


def test_enabled_database_backup_registers_single_replaceable_job(monkeypatch) -> None:
    trigger = object()
    monkeypatch.setattr(manifest_module.settings, "DB_BACKUP_ENABLE", True)
    monkeypatch.setattr(manifest_module.settings, "DB_BACKUP_CRON", "0 3 * * *")
    monkeypatch.setattr(
        manifest_module.TimerUtils,
        "build_schedule_trigger",
        Mock(return_value=trigger),
    )

    jobs = _backup_jobs(monkeypatch)

    assert [job.id for job in jobs] == ["database_backup"]
    assert len(jobs[0].triggers) == 1
    assert jobs[0].triggers[0].trigger is trigger
    assert jobs[0].triggers[0].replace_existing is True


def test_scheduled_backup_uses_registered_database_governance(monkeypatch) -> None:
    governance = Mock()
    monkeypatch.setattr(systemjobs_module, "get_database_governance", lambda: governance)

    result = systemjobs_module.database_backup()

    assert result is governance.create_backup.return_value
    governance.create_backup.assert_called_once_with()


def test_scheduler_database_dependencies_are_explicit_module_imports() -> None:
    # 源码位置从已导入的模块取，路径不写死：模块搬家时断言随之移动，
    # 而不是继续读一个不存在的路径或静默读到旧副本。
    sources = (
        Path(composition_module.__file__),
        Path(systemjobs_module.__file__),
    )
    function_imports = [
        node
        for source in sources
        for function in ast.walk(ast.parse(source.read_text(encoding="utf-8")))
        if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef))
        for node in ast.walk(function)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        and getattr(node, "module", "")
        and str(getattr(node, "module", "")).startswith("app.application.database")
    ]
    assert function_imports == []
