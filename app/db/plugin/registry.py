"""插件数据库注册表：按插件标识管理句柄的建立、建表与释放。"""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from pathlib import Path

from sqlalchemy import event, text
from sqlalchemy.engine import Connection
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
# 释放必须串行，否则同一插件会同时存在两个各持一份连接池的引擎。销毁的关连接与删载体
# 同样在锁内完成：只要在删载体前放锁，并发的 get_database 就能注册一个指向同一载体的
# 新句柄，紧接着的删除会把这个新句柄的库连同数据一起抹掉
_lock = threading.RLock()
_handles: dict[str, PluginDatabaseHandle] = {}


def _is_postgresql() -> bool:
    """判断宿主当前是否使用 PostgreSQL。"""
    db_type: str = get_runtime_setting("DB_TYPE")
    return db_type.lower() == "postgresql"


def _search_path_setter(schema: str) -> Callable[[Connection], None]:
    """
    构造把事务内未限定标识符解析到插件 schema 的 ``begin`` 监听器。

    ``schema_translate_map`` 只改写 SQLAlchemy 生成的 schema 感知语句，``text()`` 一类
    的原生 SQL 不在其列；不动 ``search_path``，插件按合同写的未限定原生 SQL 会落到
    ``public``。解析根只放插件自己的 schema，不留 ``public`` 兜底：留了兜底，插件 schema
    里不存在的表名会继续解析到宿主同名表，一条未限定的 ``DELETE`` 就能改到宿主数据。
    ``SET LOCAL`` 的作用域随事务结束，不会经连接池把插件的解析根泄漏给宿主或其它插件。
    :param schema: 插件 schema 名
    :return: 绑定该 schema 的 ``begin`` 事件监听器
    """

    def _apply_search_path(connection: Connection) -> None:
        """在事务开始时把该连接的未限定标识符解析根切到插件 schema。"""
        connection.exec_driver_sql(f'SET LOCAL search_path TO "{schema}"')

    return _apply_search_path


def _build_handle(plugin_id: str) -> PluginDatabaseHandle:
    """
    构造插件的数据库句柄。

    SQLite 每插件一个独立库文件，句柄独占引擎；PostgreSQL 复用宿主引擎并按
    ``schema_translate_map`` 派生出限定单一 schema 的外观，句柄不拥有该引擎，并在派生
    引擎上按事务限定 ``search_path``，让未限定的原生 SQL 同样落在插件自己的 schema。
    :param plugin_id: 插件标识
    :return: 新建的数据库句柄
    """
    if _is_postgresql():
        schema = plugin_schema_name(plugin_id)
        host_engine = get_engine()
        with host_engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
        engine = host_engine.execution_options(schema_translate_map={None: schema})
        # 监听器只挂在派生引擎上：OptionEngine 自带 dispatch，仅单向 _join 宿主引擎已有
        # 的监听器，宿主与其它插件的连接不会因此改变解析根
        event.listen(engine, "begin", _search_path_setter(schema))
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


def _close_handle(handle: PluginDatabaseHandle) -> None:
    """
    先关闭线程局部会话再释放句柄独占的连接池。

    线程局部会话各自扣着一条连接，不先归还就 dispose，会让已经被移出注册表的句柄仍然
    握着文件描述符。两步都是收尾操作，任一步失败只记日志：一个插件的连接池故障不得让
    宿主引擎与其余插件的释放整体失败。
    :param handle: 数据库句柄
    """
    try:
        handle.scoped_session_factory.remove()
    except Exception as error:  # noqa: BLE001  会话清理故障不得阻断连接池释放
        logger.warning(f"清理插件 {handle.plugin_id} 的线程局部会话失败：{error}")
    try:
        handle.dispose()
    except Exception as error:  # noqa: BLE001  单个插件的释放故障不得阻断其余释放
        logger.warning(f"释放插件 {handle.plugin_id} 的数据库连接失败：{error}")


def get_database(plugin_id: str) -> PluginDatabaseHandle:
    """
    取插件的数据库句柄，句柄不存在时按需建立。

    不要求插件先声明模型：只打算执行原生 SQL 的插件同样可以直接取会话。PostgreSQL 下
    句柄的会话与连接在每个事务开始时把 ``search_path`` 限定到插件 schema，未限定的原生
    SQL 因此同样解析到插件自己的表。
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
    # 静态类型系统看不到这层映射。单表继承的父类与子类共享同一个 __table__，按对象去重
    # 后才不会把同一张表提交给 create_all 两次
    tables = list(dict.fromkeys(model.__table__ for model in models))  # type: ignore[attr-defined]
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
    :raise ValueError: 声明的迁移目录不是绝对路径
    :raise FileNotFoundError: 声明的迁移目录不存在
    """
    if migrations is not None:
        # 目录校验必须早于建句柄：alembic 找不到 script_location 时抛错，而句柄已经把
        # 库文件建了出来，插件下次启动面对的是一个既没有表、也没有版本号的空库
        # 相对路径按宿主进程的当前工作目录解析，插件预期的脚本与实际执行的脚本可能是
        # 两条不同的迁移链，宁可拒绝也不能让它建出错误的表结构与版本记录
        if not migrations.is_absolute():
            raise ValueError(f"插件 {plugin_id} 声明的迁移目录不是绝对路径：{migrations}")
        if not migrations.is_dir():
            raise FileNotFoundError(f"插件 {plugin_id} 声明的迁移目录不存在：{migrations}")
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
    也不能触碰宿主连接池。关连接与摘句柄在同一把锁内完成，句柄一旦离开注册表就不会再
    被任何线程取到一个正在关闭的连接池。
    :param plugin_id: 插件标识
    """
    with _lock:
        handle = _handles.pop(plugin_id, None)
        if handle is not None:
            _close_handle(handle)


def release_all_databases() -> None:
    """释放全部插件的数据库连接，供进程关停使用。"""
    with _lock:
        plugin_ids = list(_handles)
    for plugin_id in plugin_ids:
        try:
            release_database(plugin_id)
        except Exception as error:  # noqa: BLE001  单个插件的故障不得阻断宿主引擎释放
            logger.warning(f"释放插件 {plugin_id} 的数据库失败：{error}")


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


def _remove_storage(plugin_id: str, handle: PluginDatabaseHandle | None) -> None:
    """
    删除插件数据库的持久载体：SQLite 删库文件与边车，PostgreSQL 丢弃 schema。
    :param plugin_id: 插件标识
    :param handle: 已释放的句柄，为空时按插件标识重新推导载体位置
    """
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


def destroy_database(plugin_id: str) -> None:
    """
    销毁插件的数据库：SQLite 删除库文件与 -wal/-shm 边车，PostgreSQL 丢弃对应 schema。

    不可逆，调用方必须确认处在「删除插件数据」而非「停止插件」的路径上。摘句柄、关连接
    与删载体全程持同一把锁，销毁与同一插件的句柄重建因此互斥，删除不会落到并发建出来的
    新句柄头上。失败只记日志：删除数据是一次收尾操作，把文件系统或数据库的清理故障升级
    成异常，只会让已经删掉的宿主数据与仍然存在的插件库停在不一致的中间态。
    :param plugin_id: 插件标识
    """
    with _lock:
        handle = _handles.pop(plugin_id, None)
        if handle is not None:
            _close_handle(handle)
        _remove_storage(plugin_id, handle)
