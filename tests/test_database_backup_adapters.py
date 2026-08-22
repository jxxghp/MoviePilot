from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

from app.adapters.system.backup.database import (
    PostgreSQLBackupBackend,
    SQLiteBackupBackend,
    verify_database_backup,
)


def test_sqlite_backup_includes_committed_wal_data(tmp_path: Path) -> None:
    source = tmp_path / "user.db"
    with sqlite3.connect(source) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE entries (value TEXT NOT NULL)")
        connection.execute("INSERT INTO entries VALUES ('from-wal')")

    engine = create_engine(f"sqlite:///{source}")
    backend = SQLiteBackupBackend(engine)
    artifact = tmp_path / "backup.db"

    backend.create(artifact)

    assert backend.verify(artifact).valid is True
    with sqlite3.connect(artifact) as connection:
        assert connection.execute("SELECT value FROM entries").fetchone() == ("from-wal",)
    engine.dispose()


def test_sqlite_backup_can_be_verified_without_active_engine(tmp_path: Path) -> None:
    """Doctor 等离线入口不应为校验备份而构造活动数据库引擎。"""
    artifact = tmp_path / "backup.db"
    with sqlite3.connect(artifact) as connection:
        connection.execute("CREATE TABLE entries (value TEXT NOT NULL)")

    assert verify_database_backup(artifact, db_type="sqlite").valid is True


def test_sqlite_restore_replaces_database_and_removes_old_wal_files(tmp_path: Path) -> None:
    source = tmp_path / "user.db"
    backup = tmp_path / "backup.db"
    for path, value in ((source, "old"), (backup, "restored")):
        with sqlite3.connect(path) as connection:
            connection.execute("CREATE TABLE entries (value TEXT NOT NULL)")
            connection.execute("INSERT INTO entries VALUES (?)", (value,))
    source.with_name("user.db-wal").write_bytes(b"old wal")
    source.with_name("user.db-shm").write_bytes(b"old shm")

    backend = SQLiteBackupBackend(create_engine(f"sqlite:///{source}"))
    backend.restore(backup)

    with sqlite3.connect(source) as connection:
        assert connection.execute("SELECT value FROM entries").fetchone() == ("restored",)
    assert not source.with_name("user.db-wal").exists()
    assert not source.with_name("user.db-shm").exists()


class _Runner:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, command, **kwargs):
        command = list(command)
        self.calls.append((command, kwargs))
        if "--file" in command:
            Path(command[command.index("--file") + 1]).write_bytes(b"PGDMP")
        stdout = "; archive listing" if command[:2] == ["pg_restore", "--list"] else ""
        return subprocess.CompletedProcess(command, 0, stdout, "")


class _FailedRunner:
    def __call__(self, command, **_kwargs):
        return subprocess.CompletedProcess(command, 1, "", "invalid archive")


def _postgres_backend(runner: _Runner) -> PostgreSQLBackupBackend:
    engine = SimpleNamespace(
        url=make_url(
            "postgresql://moviepilot:secret@database.internal:5432/moviepilot"
            "?sslmode=require"
        ),
        dispose=Mock(),
    )
    return PostgreSQLBackupBackend(
        engine,
        runner=runner,
        tool_resolver=lambda executable: executable,
    )


def test_postgresql_backup_keeps_password_out_of_command_and_file(tmp_path: Path) -> None:
    runner = _Runner()
    backend = _postgres_backend(runner)
    artifact = tmp_path / "backup.dump"

    backend.create(artifact)

    command, kwargs = runner.calls[0]
    assert "--format=custom" in command
    assert "secret" not in " ".join(command)
    assert kwargs["env"]["PGPASSWORD"] == "secret"
    assert kwargs["env"]["PGSSLMODE"] == "require"
    assert artifact.read_bytes() == b"PGDMP"


def test_postgresql_verify_and_restore_use_pg_restore(tmp_path: Path) -> None:
    runner = _Runner()
    backend = _postgres_backend(runner)
    artifact = tmp_path / "backup.dump"
    artifact.write_bytes(b"PGDMP")

    assert backend.verify(artifact).valid is True
    backend.restore(artifact)

    verify_command, verify_kwargs = runner.calls[0]
    restore_command, restore_kwargs = runner.calls[1]
    assert verify_command == ["pg_restore", "--list", str(artifact)]
    assert "PGPASSWORD" not in verify_kwargs["env"]
    assert "--single-transaction" in restore_command
    assert "--clean" in restore_command
    assert restore_kwargs["env"]["PGPASSWORD"] == "secret"


def test_postgresql_backup_can_be_verified_without_active_engine(tmp_path: Path) -> None:
    """PostgreSQL 归档校验只依赖 pg_restore，不连接活动数据库。"""
    runner = _Runner()
    artifact = tmp_path / "backup.dump"
    artifact.write_bytes(b"PGDMP")

    result = verify_database_backup(
        artifact,
        db_type="postgresql",
        runner=runner,
        tool_resolver=lambda executable: executable,
    )

    assert result.valid is True
    command, kwargs = runner.calls[0]
    assert command == ["pg_restore", "--list", str(artifact)]
    assert "PGPASSWORD" not in kwargs["env"]
    assert "PGSSLMODE" not in kwargs["env"]


def test_postgresql_offline_verify_rejects_invalid_archive(tmp_path: Path) -> None:
    """pg_restore 无法读取归档目录时备份必须判定为无效。"""
    artifact = tmp_path / "backup.dump"
    artifact.write_bytes(b"invalid")

    result = verify_database_backup(
        artifact,
        db_type="postgresql",
        runner=_FailedRunner(),
        tool_resolver=lambda executable: executable,
    )

    assert result.valid is False
    assert result.detail == "pg_restore 退出码 1"


def test_postgresql_offline_verify_reports_missing_client(tmp_path: Path) -> None:
    """缺少 pg_restore 时离线校验应给出可执行的安装提示。"""
    artifact = tmp_path / "backup.dump"
    artifact.write_bytes(b"PGDMP")

    with pytest.raises(RuntimeError, match="PostgreSQL client"):
        verify_database_backup(
            artifact,
            db_type="postgresql",
            tool_resolver=lambda _executable: None,
        )


def test_postgresql_source_install_reports_missing_native_client() -> None:
    runner = _Runner()
    engine = SimpleNamespace(
        url=make_url("postgresql://moviepilot:secret@database/moviepilot"),
        dispose=Mock(),
    )
    backend = PostgreSQLBackupBackend(
        engine,
        runner=runner,
        tool_resolver=lambda _executable: None,
    )

    try:
        backend.create(Path("unused.dump"))
    except RuntimeError as error:
        assert "安装与服务端同主版本或更高的 PostgreSQL client" in str(error)
    else:
        raise AssertionError("缺少 pg_dump 时未给出安装提示")
