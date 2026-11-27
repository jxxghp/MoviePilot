"""
验证 SQLite 与 PostgreSQL 备份后端的快照、恢复和离线校验。
"""

from __future__ import annotations

import sqlite3
import subprocess
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from typing import Any
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
    """
    SQLite 备份应包含已提交到 WAL 的数据。
    """
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
    """
    Doctor 等离线入口不应为校验备份而构造活动数据库引擎。
    """
    artifact = tmp_path / "backup.db"
    with sqlite3.connect(artifact) as connection:
        connection.execute("CREATE TABLE entries (value TEXT NOT NULL)")

    assert verify_database_backup(artifact, db_type="sqlite").valid is True


def test_sqlite_restore_replaces_database_and_removes_old_wal_files(tmp_path: Path) -> None:
    """
    恢复 SQLite 备份时替换数据库，并移除旧 WAL 与共享内存文件。
    """
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
    """
    记录 PostgreSQL 客户端命令并生成可校验的测试归档。
    """

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, Any]]] = []

    def __call__(
        self,
        command: list[str],
        **kwargs: Any,
    ) -> subprocess.CompletedProcess[str]:
        """
        记录命令参数并模拟成功的 pg_dump/pg_restore 结果。
        """
        command = list(command)
        self.calls.append((command, kwargs))
        if command[1:] == ["--version"]:
            return subprocess.CompletedProcess(command, 0, f"{command[0]} (PostgreSQL) 18.7", "")
        if "--file" in command:
            Path(command[command.index("--file") + 1]).write_bytes(b"PGDMP")
        stdout = "; archive listing" if command[:2] == ["pg_restore", "--list"] else ""
        return subprocess.CompletedProcess(command, 0, stdout, "")


class _FailedRunner:
    """
    模拟 PostgreSQL 客户端返回不可读取归档。
    """

    def __call__(
        self,
        command: list[str],
        **_kwargs: Any,
    ) -> subprocess.CompletedProcess[str]:
        """
        返回固定失败结果，供离线校验分支使用。
        """
        return subprocess.CompletedProcess(command, 1, "", "invalid archive")


@pytest.mark.parametrize("stderr, diagnostic", [
    ("pg_restore: error: unsupported version (1.16) in file header", "独立空库验证归档"),
    ("pg_restore: transaction failed", "pg_restore 执行失败，退出码 1"),
])
def test_postgresql_restore_failure_preserves_diagnostic(tmp_path, stderr, diagnostic):
    """
    旧客户端的归档错误提供可行的恢复路径，其他错误仍返回真实退出状态。
    """
    archive = tmp_path / "legacy.dump"
    archive.write_bytes(b"PGDMP")
    supported = _Runner()

    def runner(command, **kwargs):
        """
        只有还原命令失败，版本查询沿用真实 CLI 格式。
        """
        if "--clean" in command:
            return subprocess.CompletedProcess(command, 1, "", stderr)
        return supported(command, **kwargs)

    with pytest.raises(RuntimeError, match=diagnostic):
        _postgres_backend(runner).restore(archive)


def test_postgresql_backup_rejects_empty_archive_even_after_success(tmp_path):
    """
    进程退出成功但归档为空时，备份不能进入恢复清单。
    """
    supported = _Runner()

    def runner(command, **kwargs):
        """
        让客户端返回成功，但不产生备份数据。
        """
        result = supported(command, **kwargs)
        if "--file" in command:
            Path(command[command.index("--file") + 1]).write_bytes(b"")
        return result

    with pytest.raises(RuntimeError, match="未生成有效的备份文件"):
        _postgres_backend(runner).create(tmp_path / "empty.dump")


def _postgres_backend(runner: _Runner, *, server_major: int = 18,
                      installed_client: int | None = None) -> PostgreSQLBackupBackend:
    engine = SimpleNamespace(
        url=make_url(
            "postgresql://moviepilot:secret@database.internal:5432/moviepilot"
            "?sslmode=require"
        ),
        dispose=Mock(),
        connect=Mock(return_value=closing(SimpleNamespace(
            dialect=SimpleNamespace(server_version_info=(server_major, 14)), close=Mock(),
        ))),
    )
    def resolve(executable: str) -> str | None:
        """
        模拟 PATH 中的 18 客户端，以及可选的系统版本目录。
        """
        if not executable.startswith("/") or executable.startswith(f"/usr/lib/postgresql/{installed_client}/bin/"):
            return executable
        return None

    return PostgreSQLBackupBackend(engine, runner=runner, tool_resolver=resolve)


def test_postgresql_backup_keeps_password_out_of_command_and_file(tmp_path: Path) -> None:
    """
    PostgreSQL 密码仅通过子进程环境传入，不写入命令或备份文件。
    """
    runner = _Runner()
    backend = _postgres_backend(runner)
    artifact = tmp_path / "backup.dump"

    backend.create(artifact)

    command, kwargs = runner.calls[-1]
    assert "--format=custom" in command
    assert "secret" not in " ".join(command)
    assert kwargs["env"]["PGPASSWORD"] == "secret"
    assert kwargs["env"]["PGSSLMODE"] == "require"
    assert artifact.read_bytes() == b"PGDMP"


def test_postgresql_verify_and_restore_use_pg_restore(tmp_path: Path) -> None:
    """
    PostgreSQL 使用 pg_restore 校验归档，并在单事务中恢复。
    """
    runner = _Runner()
    backend = _postgres_backend(runner)
    artifact = tmp_path / "backup.dump"
    artifact.write_bytes(b"PGDMP")

    assert backend.verify(artifact).valid is True
    backend.restore(artifact)

    verify_command, verify_kwargs = runner.calls[0]
    restore_command, restore_kwargs = runner.calls[-1]
    assert verify_command == ["pg_restore", "--list", str(artifact)]
    assert "PGPASSWORD" not in verify_kwargs["env"]
    assert "--single-transaction" in restore_command
    assert "--clean" in restore_command
    assert restore_kwargs["env"]["PGPASSWORD"] == "secret"


def test_postgresql_16_prefers_matching_clients_for_backup_and_restore(tmp_path: Path) -> None:
    """
    服务器为 PostgreSQL 16 时使用同大版本工具，避开 PATH 中默认的 PostgreSQL 18 客户端。
    """
    runner = _Runner()
    backend = _postgres_backend(runner, server_major=16, installed_client=16)
    artifact = tmp_path / "backup.dump"

    backend.create(artifact)
    backend.restore(artifact)

    assert [cmd[0] for cmd, _ in runner.calls] == [
        "/usr/lib/postgresql/16/bin/pg_dump",
        "/usr/lib/postgresql/16/bin/pg_restore",
    ]


@pytest.mark.parametrize("server_major", [12, 13, 14, 15])
def test_older_postgresql_uses_compatible_packaged_client(tmp_path: Path, server_major: int) -> None:
    """
    升级常见旧版本时，从 PATH 中的 18 客户端回退到镜像版本目录中的 16 客户端。
    """
    runner = _Runner()
    backend = _postgres_backend(runner, server_major=server_major, installed_client=16)
    artifact = tmp_path / "backup.dump"

    backend.create(artifact)
    backend.restore(artifact)

    dump, restore = [cmd for cmd, _ in runner.calls if cmd[1:] != ["--version"]]
    assert dump[0] == "/usr/lib/postgresql/16/bin/pg_dump"
    assert restore[0] == "/usr/lib/postgresql/16/bin/pg_restore"
    assert artifact.read_bytes() == b"PGDMP"


def test_postgresql_16_rejects_unsupported_clients_before_writing(tmp_path: Path) -> None:
    """
    只有 PostgreSQL 18 工具时，数据库和归档都不应被修改。
    """
    runner = _Runner()
    backend = _postgres_backend(runner, server_major=16)
    artifact = tmp_path / "backup.dump"

    with pytest.raises(RuntimeError, match="请安装 PostgreSQL 16 客户端"):
        backend.create(artifact)
    assert not artifact.exists()
    with pytest.raises(RuntimeError, match="请安装 PostgreSQL 16 客户端"):
        backend.restore(artifact)
    assert [cmd for cmd, _ in runner.calls] == [["pg_dump", "--version"], ["pg_restore", "--version"]]
    assert all("PGPASSWORD" not in kwargs["env"] for _, kwargs in runner.calls)


def test_postgresql_backup_can_be_verified_without_active_engine(tmp_path: Path) -> None:
    """
    PostgreSQL 归档校验只依赖 pg_restore，不连接活动数据库。
    """
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
    """
    pg_restore 无法读取归档目录时备份必须判定为无效。
    """
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
    """
    缺少 pg_restore 时离线校验应给出可执行的安装提示。
    """
    artifact = tmp_path / "backup.dump"
    artifact.write_bytes(b"PGDMP")

    with pytest.raises(RuntimeError, match="PostgreSQL client"):
        verify_database_backup(
            artifact,
            db_type="postgresql",
            tool_resolver=lambda _executable: None,
        )


def test_postgresql_source_install_reports_missing_native_client() -> None:
    """
    源码安装缺少 pg_dump 时，备份入口给出客户端安装提示。
    """
    runner = _Runner()
    engine = SimpleNamespace(
        url=make_url("postgresql://moviepilot:secret@database/moviepilot"),
        dispose=Mock(),
        connect=Mock(return_value=closing(SimpleNamespace(
            dialect=SimpleNamespace(server_version_info=(16, 14)), close=Mock(),
        ))),
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
