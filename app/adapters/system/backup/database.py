"""基于活动 SQLAlchemy 引擎的 SQLite 与 PostgreSQL 备份实现。"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Protocol, Sequence

from sqlalchemy.engine import Engine


@dataclass(frozen=True, slots=True)
class DatabaseBackupCheck:
    """数据库适配器返回的基础校验结果。"""

    valid: bool
    method: str
    detail: str | None = None


class ProcessResult(Protocol):
    """数据库命令执行结果的最小合同。"""

    returncode: int
    stdout: str
    stderr: str


class ProcessRunner(Protocol):
    """可替换的数据库命令执行边界。"""

    def __call__(
        self,
        command: Sequence[str],
        *,
        env: Mapping[str, str],
        capture_output: bool,
        text: bool,
        check: bool,
    ) -> ProcessResult:
        """执行命令并返回结果。"""


def verify_database_backup(
    artifact: Path,
    *,
    db_type: str,
    runner: ProcessRunner = subprocess.run,
    tool_resolver: Callable[[str], str | None] = shutil.which,
    pg_restore: str = "pg_restore",
) -> DatabaseBackupCheck:
    """在不访问活动数据库的前提下校验一个受管备份文件。"""
    if db_type == "sqlite":
        method = "PRAGMA integrity_check"
        try:
            # 正式备份不会再变化，immutable 可避免只读校验创建 WAL 旁路文件。
            uri = f"{artifact.resolve().as_uri()}?mode=ro&immutable=1"
            with closing(sqlite3.connect(uri, uri=True)) as connection:
                rows = connection.execute("PRAGMA integrity_check").fetchall()
        except sqlite3.Error as error:
            return DatabaseBackupCheck(False, method, str(error))
        valid = bool(rows) and all(row[0] == "ok" for row in rows)
        detail = None if valid else "; ".join(str(row[0]) for row in rows)
        return DatabaseBackupCheck(valid, method, detail)

    if db_type == "postgresql":
        method = "pg_restore --list"
        executable = _require_tool(pg_restore, tool_resolver)
        result = runner(
            [executable, "--list", str(artifact)],
            env=_postgres_environment(),
            capture_output=True,
            text=True,
            check=False,
        )
        valid = result.returncode == 0 and bool(result.stdout.strip())
        detail = None if valid else f"pg_restore 退出码 {result.returncode}"
        return DatabaseBackupCheck(valid, method, detail)

    raise ValueError(f"不支持的数据库备份类型：{db_type}")


def _require_tool(
    executable: str,
    tool_resolver: Callable[[str], str | None],
) -> str:
    resolved = tool_resolver(executable)
    if resolved is None:
        raise RuntimeError(
            f"未找到 {executable}，请安装与服务端同主版本或更高的 "
            "PostgreSQL client 并加入 PATH"
        )
    return resolved


def _postgres_environment(
    *,
    password: str | None = None,
    sslmode: str | None = None,
) -> dict[str, str]:
    environment = dict(os.environ)
    environment.pop("PGPASSWORD", None)
    environment.pop("PGSSLMODE", None)
    if password:
        environment["PGPASSWORD"] = password
    if sslmode:
        environment["PGSSLMODE"] = sslmode
    return environment


class SQLiteBackupBackend:
    """使用 SQLite 在线备份 API 管理活动文件数据库。"""

    db_type = "sqlite"
    suffix = ".db"

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        database = engine.url.database
        if not database or database == ":memory:":
            raise ValueError("SQLite 内存数据库不支持文件备份")
        self._database = Path(database)

    def create(self, destination: Path) -> None:
        """从活动引擎指向的 SQLite 文件创建一致快照。"""
        source = self._engine.raw_connection()
        try:
            with closing(sqlite3.connect(destination)) as target:
                source.driver_connection.backup(target)
                target.commit()
        finally:
            source.close()

    def verify(self, artifact: Path) -> DatabaseBackupCheck:
        """通过 SQLite integrity_check 校验备份内容。"""
        return verify_database_backup(artifact, db_type=self.db_type)

    def restore(self, artifact: Path) -> None:
        """在 CLI 离线进程中原子替换活动 SQLite 文件。"""
        temporary = self._database.with_name(f".{self._database.name}.restore")
        self._engine.dispose()
        try:
            shutil.copy2(artifact, temporary)
            temporary.chmod(0o600)
            self._database.with_name(f"{self._database.name}-wal").unlink(missing_ok=True)
            self._database.with_name(f"{self._database.name}-shm").unlink(missing_ok=True)
            os.replace(temporary, self._database)
        finally:
            temporary.unlink(missing_ok=True)


class PostgreSQLBackupBackend:
    """使用 pg_dump 与 pg_restore 管理活动 PostgreSQL 数据库。"""

    db_type = "postgresql"
    suffix = ".dump"

    def __init__(
        self,
        engine: Engine,
        *,
        runner: ProcessRunner = subprocess.run,
        tool_resolver: Callable[[str], str | None] = shutil.which,
        pg_dump: str = "pg_dump",
        pg_restore: str = "pg_restore",
    ) -> None:
        self._engine = engine
        self._runner = runner
        self._tool_resolver = tool_resolver
        self._pg_dump = pg_dump
        self._pg_restore = pg_restore

    def create(self, destination: Path) -> None:
        """创建 PostgreSQL custom-format 在线备份。"""
        command = [
            self._require_tool(self._pg_dump),
            "--format=custom",
            "--no-owner",
            "--no-acl",
            "--file",
            str(destination),
            *self._connection_arguments(),
        ]
        result = self._run(command, include_password=True)
        if result.returncode != 0:
            raise RuntimeError(f"pg_dump 执行失败，退出码 {result.returncode}")
        if not destination.is_file() or destination.stat().st_size == 0:
            raise RuntimeError("pg_dump 未生成有效的备份文件")

    def verify(self, artifact: Path) -> DatabaseBackupCheck:
        """通过 pg_restore 目录读取校验 custom-format 归档。"""
        return verify_database_backup(
            artifact,
            db_type=self.db_type,
            runner=self._runner,
            tool_resolver=self._tool_resolver,
            pg_restore=self._pg_restore,
        )

    def restore(self, artifact: Path) -> None:
        """在 CLI 离线进程中覆盖当前 PostgreSQL 数据库内容。"""
        command = [
            self._require_tool(self._pg_restore),
            "--clean",
            "--if-exists",
            "--no-owner",
            "--no-acl",
            "--single-transaction",
            "--exit-on-error",
            *self._connection_arguments(),
            str(artifact),
        ]
        result = self._run(command, include_password=True)
        if result.returncode != 0:
            raise RuntimeError(f"pg_restore 执行失败，退出码 {result.returncode}")

    def _connection_arguments(self) -> list[str]:
        url = self._engine.url
        host = str(url.query.get("host") or url.host or "")
        port = str(url.query.get("port") or url.port or "")
        arguments = [
            "--username",
            str(url.username or ""),
            "--dbname",
            str(url.database or ""),
        ]
        if host:
            arguments.extend(["--host", host])
        if port:
            arguments.extend(["--port", port])
        return arguments

    def _run(self, command: Sequence[str], *, include_password: bool) -> ProcessResult:
        return self._runner(
            command,
            env=self._environment(include_password=include_password),
            capture_output=True,
            text=True,
            check=False,
        )

    def _require_tool(self, executable: str) -> str:
        return _require_tool(executable, self._tool_resolver)

    def _environment(self, *, include_password: bool) -> dict[str, str]:
        password = (
            str(self._engine.url.password)
            if include_password and self._engine.url.password
            else None
        )
        sslmode = self._engine.url.query.get("sslmode")
        return _postgres_environment(
            password=password,
            sslmode=str(sslmode) if sslmode else None,
        )
