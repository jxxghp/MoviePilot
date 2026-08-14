"""
数据库引擎的构建与连接额度核算。

同步引擎在导入期创建一次；异步引擎分两类——按事件循环池化的引擎由 session 模块
按需创建，未池化的全局引擎在此创建。两者的构建参数在这里收口。
"""
import asyncio
from typing import Dict, cast

from sqlalchemy import NullPool, QueuePool, create_engine, text
from sqlalchemy.engine import Engine as SyncEngine
from sqlalchemy.ext.asyncio import AsyncEngine as SaAsyncEngine, create_async_engine

from app.runtime.config import settings
from app.db.diagnostics import _register_database_error_logging
from app.runtime.log import logger


def _async_pool_kwargs(pooled: bool) -> dict:
    """
    异步引擎的连接池参数。

    池化时不指定 poolclass：SQLAlchemy 会自动选用异步适配的
    AsyncAdaptedQueuePool，显式传入同步的 QueuePool 反而会出错。
    :param pooled: 是否启用连接池
    :return: 传给 create_async_engine 的池参数
    """
    if not pooled:
        return {"poolclass": NullPool}
    return {
        "pool_size": settings.DB_ASYNC_POOL_SIZE,
        "max_overflow": settings.DB_ASYNC_MAX_OVERFLOW,
        "pool_timeout": settings.DB_POOL_TIMEOUT,
    }


def _get_database_engine(is_async: bool = False, pooled: bool = False):
    """
    获取数据库连接参数并设置WAL模式
    :param is_async: 是否创建异步引擎，True - 异步引擎, False - 同步引擎
    :param pooled: 异步引擎是否启用连接池，仅对常驻事件循环使用
    :return: 返回对应的数据库引擎
    """
    # 根据数据库类型选择连接方式
    if settings.DB_TYPE.lower() == "postgresql":
        return _get_postgresql_engine(is_async, pooled=pooled)
    else:
        return _get_sqlite_engine(is_async, pooled=pooled)


def _get_sqlite_engine(is_async: bool = False, pooled: bool = False):
    """
    获取SQLite数据库引擎
    """
    # 连接参数
    _connect_args = {
        "timeout": settings.DB_TIMEOUT,
    }
    # 允许部署侧注入驱动级参数（如 PgBouncer 事务模式下的 statement_cache_size）
    _connect_args.update(settings.DB_CONNECT_ARGS or {})
    # 启用 WAL 模式时的额外配置
    if settings.DB_WAL_ENABLE:
        _connect_args["check_same_thread"] = False

    # 创建同步引擎
    if not is_async:
        # 根据池类型设置 poolclass 和相关参数
        _pool_class = NullPool if settings.DB_POOL_TYPE == "NullPool" else QueuePool

        # 数据库参数
        _db_kwargs = {
            "url": settings.DB_SQLITE_URL(),
            "pool_pre_ping": settings.DB_POOL_PRE_PING,
            "echo": settings.DB_ECHO,
            "poolclass": _pool_class,
            "pool_recycle": settings.DB_POOL_RECYCLE,
            "connect_args": _connect_args
        }

        # 当使用 QueuePool 时，添加 QueuePool 特有的参数
        if _pool_class == QueuePool:
            _db_kwargs.update({
                "pool_size": settings.DB_SQLITE_POOL_SIZE,
                "pool_timeout": settings.DB_POOL_TIMEOUT,
                "max_overflow": settings.DB_SQLITE_MAX_OVERFLOW
            })

        # 创建数据库引擎
        engine = create_engine(**_db_kwargs)
        _register_database_error_logging(engine)

        # 设置WAL模式
        _journal_mode = "WAL" if settings.DB_WAL_ENABLE else "DELETE"
        with engine.connect() as connection:
            current_mode = connection.execute(text(f"PRAGMA journal_mode={_journal_mode};")).scalar()
            print(f"SQLite database journal mode set to: {current_mode}")

        return engine
    else:
        # 数据库参数，只能使用 NullPool
        _db_kwargs = {
            "url": settings.DB_SQLITE_URL("aiosqlite"),
            "pool_pre_ping": settings.DB_POOL_PRE_PING,
            "echo": settings.DB_ECHO,
            "pool_recycle": settings.DB_POOL_RECYCLE,
            "connect_args": _connect_args,
            **_async_pool_kwargs(pooled),
        }
        # 创建异步数据库引擎
        async_engine = create_async_engine(**_db_kwargs)
        _register_database_error_logging(async_engine.sync_engine)

        # 设置WAL模式
        _journal_mode = "WAL" if settings.DB_WAL_ENABLE else "DELETE"

        async def set_async_wal_mode():
            """
            设置异步引擎的WAL模式
            """
            async with async_engine.connect() as _connection:
                result = await _connection.execute(text(f"PRAGMA journal_mode={_journal_mode};"))
                _current_mode = result.scalar()
                print(f"Async SQLite database journal mode set to: {_current_mode}")

        # journal_mode 是数据库文件级的持久属性，同步引擎在导入期已经设置过。
        # 池化引擎是在运行中的事件循环里按需创建的，此处的 asyncio.run() 必然抛
        # "cannot be called from a running event loop"；而且重复设置本身也是冗余的，
        # 因此仅对导入期创建的全局引擎执行
        if not pooled:
            try:
                asyncio.run(set_async_wal_mode())
            except Exception as e:
                print(f"Failed to set async SQLite WAL mode: {e}")
        else:
            set_async_wal_mode().close()

        return async_engine


def _get_postgresql_engine(is_async: bool = False, pooled: bool = False):
    """
    获取PostgreSQL数据库引擎
    """
    db_url = settings.DB_POSTGRESQL_URL()

    # PostgreSQL连接参数。允许部署侧注入驱动级参数，
    # 例如经 PgBouncer 事务模式接入时 asyncpg 需要 statement_cache_size=0
    _connect_args = dict(settings.DB_CONNECT_ARGS or {})

    # 创建同步引擎
    if not is_async:
        # 根据池类型设置 poolclass 和相关参数
        _pool_class = NullPool if settings.DB_POOL_TYPE == "NullPool" else QueuePool

        # 数据库参数
        _db_kwargs = {
            "url": db_url,
            "pool_pre_ping": settings.DB_POOL_PRE_PING,
            "echo": settings.DB_ECHO,
            "poolclass": _pool_class,
            "pool_recycle": settings.DB_POOL_RECYCLE,
            "connect_args": _connect_args
        }

        # 当使用 QueuePool 时，添加 QueuePool 特有的参数
        if _pool_class == QueuePool:
            _db_kwargs.update({
                "pool_size": settings.DB_POSTGRESQL_POOL_SIZE,
                "pool_timeout": settings.DB_POOL_TIMEOUT,
                "max_overflow": settings.DB_POSTGRESQL_MAX_OVERFLOW
            })

        # 创建数据库引擎
        engine = create_engine(**_db_kwargs)
        _register_database_error_logging(engine)
        print(f"PostgreSQL database connected to {settings.DB_POSTGRESQL_TARGET}/{settings.DB_POSTGRESQL_DATABASE}")

        return engine
    else:
        async_db_url = settings.DB_POSTGRESQL_URL("asyncpg")

        # 数据库参数，只能使用 NullPool
        _db_kwargs = {
            "url": async_db_url,
            "pool_pre_ping": settings.DB_POOL_PRE_PING,
            "echo": settings.DB_ECHO,
            "pool_recycle": settings.DB_POOL_RECYCLE,
            "connect_args": _connect_args,
            **_async_pool_kwargs(pooled),
        }
        # 创建异步数据库引擎
        async_engine = create_async_engine(**_db_kwargs)
        _register_database_error_logging(async_engine.sync_engine)
        print(f"Async PostgreSQL database connected to {settings.DB_POSTGRESQL_TARGET}/{settings.DB_POSTGRESQL_DATABASE}")

        return async_engine


# 同步数据库引擎
# 工厂按 is_async 返回两类引擎，静态类型只能推出联合类型；这里断言各自的实际类型，
# 否则 Base.metadata.create_all(bind=Engine) 之类只收同步引擎的调用处会一路报错
Engine: SyncEngine = cast(SyncEngine, _get_database_engine(is_async=False))

# 异步数据库引擎（未池化的全局引擎，供非常驻事件循环回退使用）
AsyncEngine: SaAsyncEngine = cast(SaAsyncEngine, _get_database_engine(is_async=True))

def _async_pool_enabled() -> bool:
    """
    是否启用异步连接池。设为 NullPool 可回退到池化前的行为。
    """
    return str(settings.DB_ASYNC_POOL_TYPE or "").strip().lower() != "nullpool"


def connection_budget() -> Dict[str, int]:
    """
    核算数据库连接的理论峰值。

    各连接池此前是彼此独立配置的，没有任何地方核算总和——异步侧从无界收敛到有界
    之后，真正决定安全与否的就变成了「同步池 + 异步池 + 回退配额」这个总数是否
    还在数据库的额度之内。这里把它显式算出来，供启动校验与排障使用。
    :return: 各项上限与合计
    """
    if settings.DB_TYPE.lower() == "postgresql":
        sync_max = settings.DB_POSTGRESQL_POOL_SIZE + settings.DB_POSTGRESQL_MAX_OVERFLOW
    else:
        sync_max = settings.DB_SQLITE_POOL_SIZE + settings.DB_SQLITE_MAX_OVERFLOW
    if settings.DB_POOL_TYPE == "NullPool":
        # 同步侧也可能被配成 NullPool，此时同样无界，用线程池规模作为可观测的上限估计
        sync_max = settings.CONF.threadpool
    async_max = (settings.DB_ASYNC_POOL_SIZE + settings.DB_ASYNC_MAX_OVERFLOW
                 if _async_pool_enabled() else 0)
    fallback = settings.DB_ASYNC_FALLBACK_LIMIT if _async_pool_enabled() else settings.CONF.scheduler
    return {
        "sync": sync_max,
        "async_pooled": async_max,
        "async_fallback": fallback,
        "total": sync_max + async_max + fallback,
    }


def check_connection_budget() -> bool:
    """
    对照数据库的真实连接上限校验理论峰值，超限时告警。

    只对 PostgreSQL 生效：SQLite 没有服务端连接上限，其压力体现为 WAL 写争用而非
    连接耗尽。用真实的 max_connections 而不是猜测值——部署方可能已经调过它。
    :return: 是否在额度之内
    """
    budget = connection_budget()
    if settings.DB_TYPE.lower() != "postgresql":
        logger.info(f"数据库连接理论峰值: {budget['total']} "
                    f"(同步 {budget['sync']} + 异步池 {budget['async_pooled']} "
                    f"+ 回退 {budget['async_fallback']})")
        return True
    try:
        with Engine.connect() as conn:  # noqa
            max_conn = int(conn.execute(text("SHOW max_connections")).scalar() or 0)
            reserved = int(
                conn.execute(text("SHOW superuser_reserved_connections")).scalar() or 0
            )
    except Exception as err:
        logger.warn(f"无法读取 PostgreSQL 连接上限，跳过额度校验: {err}")
        return True
    available = max_conn - reserved
    total = budget["total"]
    detail = (f"理论峰值 {total} (同步 {budget['sync']} + 异步池 {budget['async_pooled']} "
              f"+ 回退 {budget['async_fallback']})，数据库可用 {available} "
              f"(max_connections {max_conn} - 保留 {reserved})")
    if total > available:
        logger.error(
            f"数据库连接额度不足：{detail}。"
            f"突发并发时可能出现 TooManyConnectionsError。"
            f"请调大 max_connections，或调小 DB_POSTGRESQL_MAX_OVERFLOW / "
            f"DB_ASYNC_MAX_OVERFLOW / DB_ASYNC_FALLBACK_LIMIT"
        )
        return False
    logger.info(f"数据库连接额度校验通过：{detail}")
    return True
