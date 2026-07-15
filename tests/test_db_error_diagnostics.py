import asyncio

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

import app.db as db_module


class _SqliteError(Exception):
    """模拟 sqlite3 异常暴露的扩展错误字段。"""

    sqlite_errorcode = 266
    sqlite_errorname = "SQLITE_IOERR_READ"


class _PsycopgError(Exception):
    """模拟 psycopg2 异常暴露的 SQLSTATE 字段。"""

    pgcode = "40001"


class _AsyncpgError(Exception):
    """模拟 asyncpg 适配异常暴露的 SQLSTATE 字段。"""

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
    """诊断元数据应兼容 SQLite、psycopg2 与 asyncpg 的稳定错误字段。"""
    assert db_module._database_error_metadata(error) == expected


def test_database_error_listener_omits_statement_and_parameters(monkeypatch) -> None:
    """数据库错误日志不得包含 SQL、参数或驱动返回的原始消息。"""
    messages = []
    engine = create_engine("sqlite:///:memory:")
    monkeypatch.setattr("app.db.logger.error", messages.append)
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
    """异步 Engine 应通过底层 sync engine 记录驱动错误码。"""
    messages = []
    monkeypatch.setattr("app.db.logger.error", messages.append)

    async def query_missing_table() -> None:
        async with db_module.AsyncEngine.connect() as connection:
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
    """没有驱动错误码时不应制造无效诊断日志。"""
    assert db_module._database_error_metadata(RuntimeError("plain failure")) is None
