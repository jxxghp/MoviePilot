"""
数据库错误诊断。

把驱动层的错误分类字段（sqlite3 / psycopg2 / asyncpg 各不相同）提取成统一结构，
并挂到引擎的异常事件上，使排障不依赖于阅读原始驱动异常。
"""
from typing import Any, Optional

from sqlalchemy import event
from sqlalchemy.engine import Engine as SQLAlchemyEngine, ExceptionContext

from app.runtime.log import logger


def _database_error_metadata(error: BaseException) -> Optional[dict[str, Any]]:
    """提取 SQLite 与 PostgreSQL 驱动提供的稳定错误分类字段。"""
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
    """记录非敏感驱动错误码，并保持 SQLAlchemy 原有异常传播。"""
    metadata = _database_error_metadata(exception_context.original_exception)
    if not metadata:
        return

    dialect = exception_context.dialect
    fields = {
        "database": dialect.name,
        "driver": dialect.driver,
        **metadata,
    }
    logger.error(
        "数据库驱动异常：" + ", ".join(f"{key}={value}" for key, value in fields.items())
    )


def _register_database_error_logging(engine: SQLAlchemyEngine) -> None:
    """为主程序 Engine 注册统一的底层驱动错误诊断。"""
    event.listen(engine, "handle_error", _log_database_error)
