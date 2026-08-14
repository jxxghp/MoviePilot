"""
数据库包入口。

本模块只做符号再导出，不承载实现——具体职责分布在：

- diagnostics  驱动错误的统一分类与日志
- engine       引擎构建、连接额度核算
- session      会话获取、异步连接池与配额
- decorators   同步/异步事务装饰器
- base         ORM 基类与数据访问基类
- models       表结构声明，一实体一文件
- oper         数据访问实现，与 models 同名文件一一对应

历史上这些代码全部堆在本文件里（782 行），既让包入口承担了实现职责、
使依赖图难以理清，也让「import 即建立数据库连接」这一副作用被固化下来。
"""
from typing import TYPE_CHECKING, Any

from app.db.base import Base, DbOper, execute_dml, get_id_column
from app.db.decorators import async_db_query, async_db_update, db_query, db_update
from app.db.engine import (
    check_connection_budget,
    connection_budget,
    get_engine,
    get_global_async_engine,
)
from app.db.session import (
    AsyncSessionFactory,
    ScopedSession,
    SessionFactory,
    async_session_scope,
    close_database,
    get_async_db,
    get_async_engine,
    get_async_session_factory,
    get_db,
    get_scoped_session,
    get_session_factory,
)

# ==================== 对外契约的分层 ====================
# 下方 __all__ 是本包**对外承诺**的那一层，仓库外的插件只应依赖其中的名字：
#
# - 数据访问：继承 DbOper 子类（插件基类已备好 self.plugindata / self.systemconfig），
#   或给自己的函数套 db_query / db_update / async_db_query / async_db_update 装饰器。
#   会话的获取、提交、回滚、释放全部由装饰器收口。
# - 引擎：Engine / AsyncEngine 保留在契约内。建表、Alembic 迁移、连接诊断这些用途
#   确实需要引擎对象本身，装饰器覆盖不到，仓库外拿它是正当的。
#
# SessionFactory / AsyncSessionFactory / ScopedSession 三个名字**不在**契约内，已从
# __all__ 移除，降级为内部实现细节。它们建出来的是绕过上述装饰器的裸会话——没有提交、
# 没有回滚、没有释放，谁建谁自己兜底，本身就是误用的形状。仓库内确有几处直接
# `from app.db import SessionFactory`（scheduler、postgresql 模块、Alembic 迁移脚本），
# 那是包内部的既有用法，直接导入不受 __all__ 约束，照常可用。
# 若确实需要真正的工厂对象（而非 `X()` 取一个会话），用 get_session_factory() /
# get_scoped_session() / get_async_session_factory()——转发函数上没有 sessionmaker
# 与 scoped_session 的实例接口（.remove() / .configure() / .begin() 等）。
#
# 实现上，三个工厂名字本身就是转发函数（见 session 模块），直接再导出即可——导入它们
# 不会碰引擎。Engine / AsyncEngine 则不同：调用方拿到的必须是引擎**对象**而非函数，
# 所以只能靠模块级 __getattr__ 在取属性时才创建。
#
# 注意这意味着 `from app.db import Engine` 仍会在 import 期把引擎建出来——那是调用方
# 自己选的时机。本包自身及仓库内代码一律用 get_engine()，所以 `import app.db` 不连库。
if TYPE_CHECKING:
    # 只为静态检查声明这两个名字：运行期由下方 __getattr__ 解析，模块 __dict__ 里并不存在，
    # 类型检查器无从知道它们属于本模块（__all__ 里的它们会被报成 reportUnsupportedDunderAll）。
    # 这里同时把类型钉准，比 __getattr__ 的 Any 更有用：调用方拿到的确实是这两类引擎。
    from sqlalchemy.engine import Engine as _SyncEngine
    from sqlalchemy.ext.asyncio import AsyncEngine as _SaAsyncEngine

    Engine: _SyncEngine
    AsyncEngine: _SaAsyncEngine


def __getattr__(name: str) -> Any:
    """
    惰性解析 Engine / AsyncEngine 两个旧名字，保持仓库外插件的导入路径可用。
    :param name: 属性名
    :return: 对应的引擎
    """
    if name == "Engine":
        return get_engine()
    if name == "AsyncEngine":
        return get_global_async_engine()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


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
