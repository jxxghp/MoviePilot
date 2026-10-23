"""
数据库错误诊断。

把驱动层的错误分类字段（sqlite3 / psycopg2 / asyncpg 各不相同）提取成统一结构，
并挂到引擎的异常事件上；SQLite I/O 故障还会使坏连接失效，避免连接池持续复用。
"""
import sqlite3
from typing import Any, Optional

from sqlalchemy import event
from sqlalchemy.engine import Engine as SQLAlchemyEngine
from sqlalchemy.engine import ExceptionContext

from app.runtime.log import logger

# SQLite 扩展错误码的低 8 位是主错误码，覆盖短读、读失败和写失败等 I/O 子类。
_SQLITE_PRIMARY_ERROR_MASK = 0xFF


def _database_error_metadata(error: BaseException) -> Optional[dict[str, Any]]:
    """
    提取 SQLite 与 PostgreSQL 驱动提供的稳定错误分类字段。
    """
    metadata = {"error_type": type(error).__name__}

    # DBAPI 驱动字段并不共享统一类型，动态读取可同时兼容 sqlite3、psycopg2 与 asyncpg。
    sqlite_errorcode = getattr(error, "sqlite_errorcode", None)
    sqlite_errorname = getattr(error, "sqlite_errorname", None)
    if sqlite_errorcode is not None or sqlite_errorname:
        if sqlite_errorcode is not None:
            metadata["error_code"] = sqlite_errorcode
        if sqlite_errorname:
            metadata["error_name"] = sqlite_errorname
        return metadata

    sqlstate = getattr(error, "sqlstate", None) or getattr(error, "pgcode", None)
    if not sqlstate:
        sqlstate = getattr(getattr(error, "diag", None), "sqlstate", None)
    if sqlstate:
        metadata["sqlstate"] = sqlstate
        return metadata

    return None


def _log_database_error(exception_context: ExceptionContext) -> None:
    """
    记录非敏感驱动错误码，并让 SQLAlchemy 淘汰发生 SQLite I/O 故障的连接。

    原事务仍向调用方抛错，不在驱动层重放写操作；后续独立事务重新连接。
    锁争用、约束冲突和 PostgreSQL 错误保持方言原有的断连判定。
    """
    metadata = _database_error_metadata(exception_context.original_exception)
    if not metadata:
        return

    dialect = exception_context.dialect
    error_code = metadata.get("error_code")
    if (
        dialect.name == "sqlite"
        and isinstance(error_code, int)
        and error_code & _SQLITE_PRIMARY_ERROR_MASK == sqlite3.SQLITE_IOERR
    ):
        exception_context.is_disconnect = True
    fields = {
        "database": dialect.name,
        "driver": dialect.driver,
        **metadata,
    }
    logger.error(
        "数据库驱动异常：" + ", ".join(f"{key}={value}" for key, value in fields.items())
    )


def _register_database_error_logging(engine: SQLAlchemyEngine) -> None:
    """
    为宿主与插件 Engine 注册统一的驱动错误诊断和 SQLite 坏连接失效策略。
    """
    event.listen(engine, "handle_error", _log_database_error)
