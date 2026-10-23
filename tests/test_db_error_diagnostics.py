"""
验证数据库错误诊断提取驱动错误码且不记录 SQL 参数。
"""

import asyncio
import sqlite3
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import create_async_engine

# 诊断实现已迁至 app.db.diagnostics；app.db 只做 re-export，私有符号不在其上
import app.db.diagnostics as db_module

# 用 getter 而不是旧名字 AsyncEngine：后者只为仓库外插件保留，模块级导入它会在 pytest
# 的收集期就把全局异步引擎建出来
from app.db.engine import get_global_async_engine


class _SqliteError(Exception):
    """
    模拟 sqlite3 异常暴露的扩展错误字段。
    """

    sqlite_errorcode = 266
    sqlite_errorname = "SQLITE_IOERR_READ"


class _PsycopgError(Exception):
    """
    模拟 psycopg2 异常暴露的 SQLSTATE 字段。
    """

    pgcode = "40001"


class _AsyncpgError(Exception):
    """
    模拟 asyncpg 适配异常暴露的 SQLSTATE 字段。
    """

    sqlstate = "23505"


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            _SqliteError("disk I/O error"),
            {
                "error_type": "_SqliteError",
                "error_code": 266,
                "error_name": "SQLITE_IOERR_READ",
            },
        ),
        (
            _PsycopgError("serialization failure"),
            {
                "error_type": "_PsycopgError",
                "sqlstate": "40001",
            },
        ),
        (
            _AsyncpgError("duplicate key"),
            {
                "error_type": "_AsyncpgError",
                "sqlstate": "23505",
            },
        ),
    ],
)
def test_database_error_metadata_extracts_driver_codes(error, expected) -> None:
    """
    诊断元数据应兼容 SQLite、psycopg2 与 asyncpg 的稳定错误字段。
    """
    assert db_module._database_error_metadata(error) == expected


def test_database_error_listener_omits_statement_and_parameters(monkeypatch) -> None:
    """
    数据库错误日志不得包含 SQL、参数或驱动返回的原始消息。
    """
    messages = []
    engine = create_engine("sqlite:///:memory:")
    monkeypatch.setattr("app.db.diagnostics.logger.error", messages.append)
    db_module._register_database_error_logging(engine)

    with pytest.raises(OperationalError):
        with engine.connect() as connection:
            connection.execute(
                text("SELECT * FROM missing_table WHERE token = :token"),
                {"token": "private-token"},
            )

    assert len(messages) == 1
    assert "database=sqlite" in messages[0]
    assert "driver=pysqlite" in messages[0]
    assert "error_code=1" in messages[0]
    assert "error_name=SQLITE_ERROR" in messages[0]
    assert "missing_table" not in messages[0]
    assert "private-token" not in messages[0]


def test_async_database_engine_logs_driver_error_metadata(monkeypatch) -> None:
    """
    异步 Engine 应通过底层 sync engine 记录驱动错误码。
    """
    messages = []
    monkeypatch.setattr("app.db.diagnostics.logger.error", messages.append)

    async def query_missing_table() -> None:
        async with get_global_async_engine().connect() as connection:
            await connection.execute(text("SELECT * FROM async_missing_table"))

    with pytest.raises(OperationalError):
        asyncio.run(query_missing_table())

    assert len(messages) == 1
    assert "database=sqlite" in messages[0]
    assert "driver=aiosqlite" in messages[0]
    assert "error_code=1" in messages[0]
    assert "error_name=SQLITE_ERROR" in messages[0]
    assert "async_missing_table" not in messages[0]


def test_database_error_metadata_ignores_unclassified_errors() -> None:
    """
    没有驱动错误码时不应制造无效诊断日志。
    """
    assert db_module._database_error_metadata(RuntimeError("plain failure")) is None


@pytest.mark.parametrize(
    ("database", "code", "invalidated"),
    [
        ("sqlite", sqlite3.SQLITE_IOERR, True),
        ("sqlite", sqlite3.SQLITE_IOERR_READ, True),
        ("sqlite", sqlite3.SQLITE_IOERR_SHORT_READ, True),
        ("sqlite", sqlite3.SQLITE_IOERR_WRITE, True),
        ("sqlite", sqlite3.SQLITE_BUSY, False),
        ("sqlite", sqlite3.SQLITE_LOCKED, False),
        ("sqlite", sqlite3.SQLITE_CONSTRAINT, False),
        ("sqlite", sqlite3.SQLITE_ERROR, False),
        ("sqlite", None, False),
        ("postgresql", sqlite3.SQLITE_IOERR, False),
    ],
)
def test_only_sqlite_io_errors_invalidate_connections(database, code, invalidated) -> None:
    """
    仅按 SQLite 稳定错误码淘汰 I/O 故障连接，不把锁争用或文本相似的错误当成断连。
    """
    error = sqlite3.OperationalError("disk I/O error")
    if code is not None:
        error.sqlite_errorcode = code
    context = SimpleNamespace(
        original_exception=error,
        dialect=SimpleNamespace(name=database, driver="test"),
        is_disconnect=False,
    )

    db_module._log_database_error(context)

    assert context.is_disconnect is invalidated


def _inject_sqlite_io_error(engine, error) -> list[str]:
    """
    在真实引擎的首条 INSERT 驱动调用处注入短读错误，后续语句继续访问临时数据库。

    :param engine: 用例独占的同步引擎，异步引擎传入其 sync_engine
    :param error: 向调用方原样传播的 SQLite 驱动异常
    :return: INSERT 尝试记录，用于验证引擎没有隐式重放写操作
    """
    attempts = []

    def fail_first_insert(_cursor, statement, _parameters, _context):
        """
        只让首次写入失败，以区分复用坏连接与换连接后的显式恢复操作。
        """
        if statement.startswith("INSERT"):
            attempts.append(statement)
            if len(attempts) == 1:
                raise error

    event.listen(engine, "do_execute", fail_first_insert)
    return attempts


def test_sqlite_io_error_releases_connection_without_replaying_write(tmp_path) -> None:
    """
    同步 I/O 故障必须向上抛出并释放坏连接，下一次独立事务换连接后才能成功写入。
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'recovery.db'}")
    db_module._register_database_error_logging(engine)
    error = sqlite3.OperationalError("disk I/O error")
    error.sqlite_errorcode = sqlite3.SQLITE_IOERR_SHORT_READ
    attempts = _inject_sqlite_io_error(engine, error)
    try:
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE recovery (value INTEGER)"))
        with engine.connect() as connection:
            original = connection.connection.dbapi_connection
            with pytest.raises(OperationalError) as failure:
                connection.execute(text("INSERT INTO recovery VALUES (1)"))
            assert failure.value.orig is error
            assert failure.value.connection_invalidated
        assert len(attempts) == 1

        with engine.begin() as connection:
            assert connection.connection.dbapi_connection is not original
            assert connection.scalar(text("SELECT COUNT(*) FROM recovery")) == 0
            connection.execute(text("INSERT INTO recovery VALUES (2)"))
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT value FROM recovery")) == 2
        assert len(attempts) == 2
        assert engine.pool.checkedout() == 0
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_async_sqlite_io_error_releases_connection_without_replaying_write(tmp_path) -> None:
    """
    异步连接池同样淘汰 I/O 故障连接，保留原始异常且不在失败事务内自动重试。
    """
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'async-recovery.db'}")
    db_module._register_database_error_logging(engine.sync_engine)
    error = sqlite3.OperationalError("disk I/O error")
    error.sqlite_errorcode = sqlite3.SQLITE_IOERR_SHORT_READ
    attempts = _inject_sqlite_io_error(engine.sync_engine, error)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("CREATE TABLE recovery (value INTEGER)"))
        async with engine.connect() as connection:
            original = (await connection.get_raw_connection()).dbapi_connection
            with pytest.raises(OperationalError) as failure:
                await connection.execute(text("INSERT INTO recovery VALUES (1)"))
            assert failure.value.orig is error
            assert failure.value.connection_invalidated
        assert len(attempts) == 1

        async with engine.begin() as connection:
            assert (await connection.get_raw_connection()).dbapi_connection is not original
            assert await connection.scalar(text("SELECT COUNT(*) FROM recovery")) == 0
            await connection.execute(text("INSERT INTO recovery VALUES (2)"))
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT value FROM recovery")) == 2
        assert len(attempts) == 2
        assert engine.sync_engine.pool.checkedout() == 0
    finally:
        await engine.dispose()
