"""数据库包的惰性兼容导出入口。

具体实现位于 ``base``、``decorators``、``engine`` 与 ``session``。包入口只维护
公开符号到所有者模块的映射，避免数据库子模块为了导入同包实现而回流到一个会主动
导入全部实现的根模块。旧的 ``from app.db import X`` 路径继续可用。
"""

from importlib import import_module
from typing import Any


_EXPORTS = {
    "AsyncSessionFactory": ("app.db.session", "AsyncSessionFactory"),
    "Base": ("app.db.base", "Base"),
    "DbOper": ("app.db.base", "DbOper"),
    "ScopedSession": ("app.db.session", "ScopedSession"),
    "SessionFactory": ("app.db.session", "SessionFactory"),
    "async_db_query": ("app.db.decorators", "async_db_query"),
    "async_db_update": ("app.db.decorators", "async_db_update"),
    "async_session_scope": ("app.db.session", "async_session_scope"),
    "check_connection_budget": ("app.db.engine", "check_connection_budget"),
    "close_database": ("app.db.session", "close_database"),
    "connection_budget": ("app.db.engine", "connection_budget"),
    "db_query": ("app.db.decorators", "db_query"),
    "db_update": ("app.db.decorators", "db_update"),
    "execute_dml": ("app.db.base", "execute_dml"),
    "get_async_db": ("app.db.session", "get_async_db"),
    "get_async_engine": ("app.db.session", "get_async_engine"),
    "get_async_session_factory": (
        "app.db.session",
        "get_async_session_factory",
    ),
    "get_db": ("app.db.session", "get_db"),
    "get_engine": ("app.db.engine", "get_engine"),
    "get_global_async_engine": ("app.db.engine", "get_global_async_engine"),
    "get_id_column": ("app.db.base", "get_id_column"),
    "get_scoped_session": ("app.db.session", "get_scoped_session"),
    "get_session_factory": ("app.db.session", "get_session_factory"),
}


def __getattr__(name: str) -> Any:
    """按需解析旧数据库导出，并缓存到包命名空间。"""
    if name == "Engine":
        return getattr(import_module("app.db.engine"), "get_engine")()
    elif name == "AsyncEngine":
        return getattr(
            import_module("app.db.engine"),
            "get_global_async_engine",
        )()
    elif name in _EXPORTS:
        module_name, symbol_name = _EXPORTS[name]
        value = getattr(import_module(module_name), symbol_name)
    else:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """向交互式工具暴露兼容符号，同时保持实现模块惰性。"""
    return sorted({*globals(), *_EXPORTS, "Engine", "AsyncEngine"})


__all__ = [
    "AsyncEngine",
    "Base",
    "DbOper",
    "Engine",
    "async_db_query",
    "async_db_update",
    "async_session_scope",
    "check_connection_budget",
    "close_database",
    "connection_budget",
    "db_query",
    "db_update",
    "execute_dml",
    "get_async_db",
    "get_async_engine",
    "get_async_session_factory",
    "get_db",
    "get_engine",
    "get_global_async_engine",
    "get_id_column",
    "get_scoped_session",
    "get_session_factory",
]
