"""插件数据库注册表：按插件标识管理句柄的建立、建表与释放。"""

from __future__ import annotations

import threading
from collections.abc import Sequence
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import scoped_session, sessionmaker

from app.db.engine import apply_sqlite_journal_mode, build_sqlite_engine, get_engine
from app.db.plugin.container import PluginDatabaseHandle
from app.db.plugin.locator import (
    plugin_schema_name,
    sqlite_database_path,
    sqlite_sidecar_paths,
)
from app.runtime.log import logger
from app.runtime.settings import get_runtime_setting

__all__ = [
    "destroy_database",
    "ensure_database",
    "get_database",
    "release_all_databases",
    "release_database",
]

# 插件的启动、停止与热重载分别来自调度线程、文件监控线程与 HTTP 线程，句柄的建立与
# 释放必须串行，否则同一插件会同时存在两个各持一份连接池的引擎
_lock = threading.RLock()
_handles: dict[str, PluginDatabaseHandle] = {}


def _is_postgresql() -> bool:
    """判断宿主当前是否使用 PostgreSQL。"""
    return str(get_runtime_setting("DB_TYPE")).lower() == "postgresql"


def _build_handle(plugin_id: str) -> PluginDatabaseHandle:
    """
    构造插件的数据库句柄。

    SQLite 每插件一个独立库文件，句柄独占引擎；PostgreSQL 复用宿主引擎并按
    ``schema_translate_map`` 派生出限定单一 schema 的外观，句柄不拥有该引擎。
    :param plugin_id: 插件标识
    :return: 新建的数据库句柄
    """
    if _is_postgresql():
        schema = plugin_schema_name(plugin_id)
        host_engine = get_engine()
        with host_engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
        engine = host_engine.execution_options(schema_translate_map={None: schema})
        db_path = None
        owns_engine = False
    else:
        db_path = sqlite_database_path(plugin_id)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        engine = build_sqlite_engine(f"sqlite:///{db_path}")
        apply_sqlite_journal_mode(engine)
        schema = None
        owns_engine = True

    session_factory = sessionmaker(bind=engine)
    return PluginDatabaseHandle(
        plugin_id=plugin_id,
        engine=engine,
        session_factory=session_factory,
        scoped_session_factory=scoped_session(session_factory),
        db_path=db_path,
        schema=schema,
        owns_engine=owns_engine,
    )


def get_database(plugin_id: str) -> PluginDatabaseHandle:
    """
    取插件的数据库句柄，句柄不存在时按需建立。

    不要求插件先声明模型：只打算执行原生 SQL 的插件同样可以直接取会话。
    :param plugin_id: 插件标识
    :return: 数据库句柄
    """
    with _lock:
        handle = _handles.get(plugin_id)
        if handle is None:
            handle = _build_handle(plugin_id)
            _handles[plugin_id] = handle
        return handle


def _create_declared_tables(handle: PluginDatabaseHandle, models: Sequence[type]) -> None:
    """
    按声明的模型建表，只创建插件显式列出的表。
    :param handle: 数据库句柄
    :param models: 插件声明的模型类
    """
    # 端口签名只承诺 type：__table__ 由 SQLAlchemy 声明式元类在类构造期动态挂载，
    # 静态类型系统看不到这层映射
    tables = [model.__table__ for model in models]  # type: ignore[attr-defined]
    for metadata in dict.fromkeys(table.metadata for table in tables):
        metadata.create_all(
            bind=handle.engine,
            tables=[table for table in tables if table.metadata is metadata],
        )


def ensure_database(
    plugin_id: str,
    models: Sequence[type] = (),
    migrations: Path | None = None,
) -> None:
    """
    按插件的声明建立数据库：声明了迁移目录走 alembic，否则按模型建表。

    两者都未声明时不建立句柄、不产生任何库文件——绝大多数插件不使用自有库，不该因为
    宿主统一调用了本函数就凭空多出一个空库和一个空目录。
    :param plugin_id: 插件标识
    :param models: 插件声明的模型类
    :param migrations: 插件声明的 Alembic 迁移目录
    """
    if migrations is not None:
        # 迁移目录同时描述建表与后续版本演进，与按模型建表会争夺同一批表，故优先且互斥。
        # alembic 只在插件真的声明了迁移目录时才需要，在函数内导入可让宿主与只声明模型的
        # 插件不为它付出导入代价
        from app.db.plugin.migration import run_migrations

        run_migrations(get_database(plugin_id), migrations)
        return
    if models:
        _create_declared_tables(get_database(plugin_id), models)


def release_database(plugin_id: str) -> None:
    """
    释放插件的数据库连接，保留全部数据。

    只 dispose 句柄自己拥有的引擎；PostgreSQL 下句柄只是宿主引擎的外观，这里不会、
    也不能触碰宿主连接池。
    :param plugin_id: 插件标识
    """
    with _lock:
        handle = _handles.pop(plugin_id, None)
    if handle is not None:
        handle.dispose()


def release_all_databases() -> None:
    """释放全部插件的数据库连接，供进程关停使用。"""
    with _lock:
        plugin_ids = list(_handles)
    for plugin_id in plugin_ids:
        release_database(plugin_id)


def _drop_schema(plugin_id: str, schema: str) -> None:
    """
    丢弃插件的 PostgreSQL schema。
    :param plugin_id: 插件标识
    :param schema: schema 名
    """
    try:
        with get_engine().begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
    except Exception as error:  # noqa: BLE001  删除数据的收尾故障不得升级为异常
        logger.error(f"销毁插件 {plugin_id} 的数据库 schema {schema} 失败：{error}")


def destroy_database(plugin_id: str) -> None:
    """
    销毁插件的数据库：SQLite 删除库文件与 -wal/-shm 边车，PostgreSQL 丢弃对应 schema。

    不可逆，调用方必须确认处在「删除插件数据」而非「停止插件」的路径上。失败只记日志：
    删除数据是一次收尾操作，把文件系统或数据库的清理故障升级成异常，只会让已经删掉的
    宿主数据与仍然存在的插件库停在不一致的中间态。
    :param plugin_id: 插件标识
    """
    with _lock:
        handle = _handles.pop(plugin_id, None)
    if handle is not None:
        handle.dispose()

    if _is_postgresql():
        # 句柄存在时 schema 必非空（PostgreSQL 分支总会填充）；句柄已释放则重新按插件标识
        # 推导，两种取值路径的类型都靠 or 收窄为确定的 str，不引入运行期新分支
        schema = (handle.schema if handle else None) or plugin_schema_name(plugin_id)
        _drop_schema(plugin_id, schema)
        return

    db_path = (handle.db_path if handle else None) or sqlite_database_path(plugin_id)
    for candidate in (db_path, *sqlite_sidecar_paths(db_path)):
        try:
            candidate.unlink(missing_ok=True)
        except OSError as error:
            logger.warning(f"删除插件 {plugin_id} 的数据库文件 {candidate} 失败：{error}")
