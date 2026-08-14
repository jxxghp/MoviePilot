"""
数据库包入口。

本模块只做符号再导出，不承载实现——具体职责分布在：

- diagnostics  驱动错误的统一分类与日志
- engine       引擎构建、连接额度核算
- session      会话获取、异步连接池与配额
- decorators   同步/异步事务装饰器
- base         ORM 基类与数据访问基类

历史上这些代码全部堆在本文件里（782 行），既让包入口承担了实现职责、
使依赖图难以理清，也让「import 即建立数据库连接」这一副作用被固化下来。
"""
from app.db.base import Base, DbOper, execute_dml, get_id_column
from app.db.decorators import async_db_query, async_db_update, db_query, db_update
from app.db.engine import (
    AsyncEngine,
    Engine,
    check_connection_budget,
    connection_budget,
)
from app.db.session import (
    AsyncSessionFactory,
    ScopedSession,
    SessionFactory,
    async_session_scope,
    close_database,
    get_async_db,
    get_async_engine,
    get_db,
)

__all__ = [
    "AsyncEngine",
    "AsyncSessionFactory",
    "Base",
    "DbOper",
    "Engine",
    "ScopedSession",
    "SessionFactory",
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
    "get_db",
    "get_id_column",
]
