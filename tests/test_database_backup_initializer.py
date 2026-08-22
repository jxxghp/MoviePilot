from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from app.application.database import get_database_governance
from app.startup.bindings import database as startup_database


def test_builder_uses_cached_engine_as_database_fact_source(monkeypatch) -> None:
    engine = SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))
    backend = Mock(db_type="sqlite", suffix=".db")
    sqlite_backend = Mock(return_value=backend)
    monkeypatch.setattr(startup_database, "get_engine", lambda: engine)
    monkeypatch.setattr(startup_database, "SQLiteBackupBackend", sqlite_backend)
    monkeypatch.setattr(startup_database.settings, "DB_TYPE", "postgresql")

    startup_database.build_database_governance()

    sqlite_backend.assert_called_once_with(engine)


def test_backup_policy_reads_current_path_and_retention(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(startup_database.settings, "DB_BACKUP_PATH", str(tmp_path))
    monkeypatch.setattr(startup_database.settings, "DB_BACKUP_RETENTION_DAYS", 7)
    monkeypatch.setattr(startup_database.settings, "DB_BACKUP_MAX_COUNT", 5)

    policy = startup_database.read_backup_policy()

    assert policy.root == tmp_path
    assert policy.retention_days == 7
    assert policy.max_count == 5


def test_configure_registers_one_database_governance(monkeypatch) -> None:
    governance = Mock()
    monkeypatch.setattr(startup_database, "build_database_governance", lambda: governance)

    startup_database.configure_database()

    assert get_database_governance() is governance
