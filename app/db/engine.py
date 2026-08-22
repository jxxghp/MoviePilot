"""
数据库引擎的构建与连接额度核算。

同步引擎与未池化的全局异步引擎都在此按需创建（首次访问时，不在 import 期）；
按事件循环池化的异步引擎由 session 模块创建。三者的构建参数在这里收口。
"""
import threading
from typing import Dict, Optional, cast

from sqlalchemy import NullPool, QueuePool, create_engine, event, text
from sqlalchemy.engine import Engine as SyncEngine
from sqlalchemy.ext.asyncio import AsyncEngine as SaAsyncEngine, create_async_engine
from sqlalchemy.pool import Pool

from app.runtime.config import settings
from app.db.diagnostics import _register_database_error_logging
from app.runtime.log import logger
from app.runtime.observability import record_metric


def _database_backend_label() -> str:
    """把数据库类型收敛为有限的观测标签。"""
    return "postgresql" if settings.DB_TYPE.lower() == "postgresql" else "sqlite"


def _register_database_pool_metrics(engine: SyncEngine) -> None:
    """在 SQLAlchemy 池 checkout/checkin 边界维护当前借出连接数。"""
    if not isinstance(engine.pool, Pool):
        # 引擎构建单测允许注入不具备 PoolEvents 的轻量替身。
        return
    backend = _database_backend_label()

    def record_checkout(*_args: object) -> None:
        """连接借出后增加当前使用量。"""
        record_metric("db.pool.checked_out", 1, backend=backend)

    def record_checkin(*_args: object) -> None:
        """连接归还后减少当前使用量。"""
        record_metric("db.pool.checked_out", -1, backend=backend)

    event.listen(engine.pool, "checkout", record_checkout)
    event.listen(engine.pool, "checkin", record_checkin)


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


def _sqlite_connect_args() -> dict:
    """
    SQLite 连接参数：驱动超时、部署侧注入的驱动级参数，以及 WAL 模式下需要放开的
    跨线程限制。
    :return: 传给 create_engine / create_async_engine 的 connect_args
    """
    _connect_args = {
        "timeout": settings.DB_TIMEOUT,
    }
    # 允许部署侧注入驱动级参数（如 PgBouncer 事务模式下的 statement_cache_size）
    _connect_args.update(settings.DB_CONNECT_ARGS or {})
    # 启用 WAL 模式时的额外配置
    if settings.DB_WAL_ENABLE:
        _connect_args["check_same_thread"] = False
    return _connect_args


def build_sqlite_engine(url: str) -> SyncEngine:
    """
    按 URL 构造同步 SQLite 引擎：连接池、错误分类日志挂载、journal_mode 设定
    一次性完成，供核心库与插件自管理库共用同一套连接语义。
    :param url: SQLite 连接 URL
    :return: 同步引擎
    """
    _connect_args = _sqlite_connect_args()

    # 根据池类型设置 poolclass 和相关参数
    _pool_class = NullPool if settings.DB_POOL_TYPE == "NullPool" else QueuePool

    # 数据库参数
    _db_kwargs = {
        "url": url,
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
    _register_database_pool_metrics(engine)

    # 设置WAL模式。
    # 这是引擎构建里唯一的阻塞 I/O。调用方若在创建锁内构建引擎（如 get_engine()
    # 的双重检查锁），journal_mode 必须有人设置一次，而阻塞的也只是本地 SQLite
    # 的一次 PRAGMA，不会有大量线程等在锁上的场面。
    _journal_mode = "WAL" if settings.DB_WAL_ENABLE else "DELETE"
    with engine.connect() as connection:
        current_mode = connection.execute(text(f"PRAGMA journal_mode={_journal_mode};")).scalar()
        print(f"SQLite database journal mode set to: {current_mode}")

    return engine


def _get_sqlite_engine(is_async: bool = False, pooled: bool = False):
    """
    获取SQLite数据库引擎
    """
    # 创建同步引擎
    if not is_async:
        return build_sqlite_engine(settings.DB_SQLITE_URL())
    else:
        # 数据库参数，只能使用 NullPool
        _db_kwargs = {
            "url": settings.DB_SQLITE_URL("aiosqlite"),
            "pool_pre_ping": settings.DB_POOL_PRE_PING,
            "echo": settings.DB_ECHO,
            "pool_recycle": settings.DB_POOL_RECYCLE,
            "connect_args": _sqlite_connect_args(),
            **_async_pool_kwargs(pooled),
        }
        # 创建异步数据库引擎
        async_engine = create_async_engine(**_db_kwargs)
        _register_database_error_logging(async_engine.sync_engine)
        _register_database_pool_metrics(async_engine.sync_engine)

        # 异步侧不再设置 WAL。journal_mode 是数据库文件级的持久属性，同步引擎已经设置过，
        # 这里重复设置本就是冗余的；而它原本用 asyncio.run() 完成，是异步引擎构建里唯一的
        # 阻塞 I/O。引擎改为惰性创建之后，构建可能发生在任意线程——包括在运行中的事件循环
        # 内部（async_session_scope 首次取全局引擎时），那里调 asyncio.run() 会直接抛
        # RuntimeError；即便不抛，它也是在持有创建锁的状态下阻塞，会把所有等锁的线程拖死。
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
        _register_database_pool_metrics(engine)
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
        _register_database_pool_metrics(async_engine.sync_engine)
        print(f"Async PostgreSQL database connected to {settings.DB_POSTGRESQL_TARGET}/{settings.DB_POSTGRESQL_DATABASE}")

        return async_engine


# 引擎按需创建，不在 import 期建立连接。
#
# 此前这两个是模块级常量，`import app.db` 就会按 settings 连库、建出 user.db、SQLite 还要
# 去设一次 WAL——仅仅把这个包 import 进来（工具脚本、子进程探测、文档生成）就有了副作用。
#
# 注意惰性化并没有让「隔离 CONFIG_DIR 必须早于 import」这条约束消失：settings 是在
# import app.runtime.config 时构造的，那一刻 CONFIG_DIR 就定型了，晚建的引擎连的仍是真实库。
# 它消掉的是「import 本身即产生副作用」，以及由此带来的「测试想换库就必须重新起进程」。
#
# 惰性化引入的唯一新风险是首次访问的并发：这个项目有上百个调度线程，创建出多个引擎
# 意味着各自持一份连接池，实际连接数是额度核算的数倍。因此用双重检查加锁收口。
_sync_engine_lock = threading.RLock()
_async_engine_lock = threading.RLock()
_sync_engine: Optional[SyncEngine] = None
_async_engine: Optional[SaAsyncEngine] = None


def get_engine() -> SyncEngine:
    """
    获取同步数据库引擎，首次调用时创建。
    :return: 同步引擎
    """
    global _sync_engine
    if _sync_engine is None:
        with _sync_engine_lock:
            # 锁内复查：等锁期间可能已被其它线程创建
            if _sync_engine is None:
                _sync_engine = cast(SyncEngine, _get_database_engine(is_async=False))
    return _sync_engine


def get_global_async_engine() -> SaAsyncEngine:
    """
    获取未池化的全局异步引擎，供非常驻事件循环回退使用，首次调用时创建。
    :return: 异步引擎
    """
    global _async_engine
    if _async_engine is None:
        with _async_engine_lock:
            if _async_engine is None:
                _async_engine = cast(SaAsyncEngine, _get_database_engine(is_async=True))
    return _async_engine


def peek_sync_engine() -> Optional[SyncEngine]:
    """
    取已创建的同步引擎，未创建时返回 None——不触发创建。

    关停路径（close_database、测试引导的 atexit）需要「有就释放、没有就算了」：
    走 get_engine() 会为了 dispose 而先连一次库，在从未用过数据库的进程里尤其荒谬。
    取锁而不是裸读槽位：另一个线程可能正卡在创建里，裸读会看到 None、把那个引擎漏掉，
    取锁则会等它建完。注意这只是把竞态窗口**缩小**，并没有消除——在 peek 返回之后才
    开始创建的引擎照样漏。真要杜绝得让关停之后的创建直接失败，那是另一层面的改动。
    :return: 同步引擎或 None
    """
    with _sync_engine_lock:
        return _sync_engine


def peek_async_engine() -> Optional[SaAsyncEngine]:
    """
    取已创建的全局异步引擎，未创建时返回 None——不触发创建。
    :return: 异步引擎或 None
    """
    with _async_engine_lock:
        return _async_engine


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
    连接池是进程级的：多 worker 部署时每个进程各持一份，因此合计要乘上 worker 数。
    只报单进程用量会让多 worker 在启动校验里一路绿灯，实际第一个 worker 还没起完
    就顶穿了 max_connections。
    :return: 单进程各项上限、worker 数与合计
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
    per_worker = sync_max + async_max + fallback
    # worker 数非法时按 1 计：退化成 0 会让合计归零、反而误判「额度充足」
    workers = getattr(settings, "API_WORKERS", 1) or 1
    workers = workers if isinstance(workers, int) and workers > 0 else 1
    return {
        "sync": sync_max,
        "async_pooled": async_max,
        "async_fallback": fallback,
        "per_worker": per_worker,
        "workers": workers,
        "total": per_worker * workers,
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
                    f"(单进程 {budget['per_worker']} = 同步 {budget['sync']} + 异步池 "
                    f"{budget['async_pooled']} + 回退 {budget['async_fallback']}"
                    f"，worker {budget['workers']})")
        return True
    try:
        with get_engine().connect() as conn:
            max_conn = int(conn.execute(text("SHOW max_connections")).scalar() or 0)
            reserved = int(
                conn.execute(text("SHOW superuser_reserved_connections")).scalar() or 0
            )
    except Exception as err:
        logger.warn(f"无法读取 PostgreSQL 连接上限，跳过额度校验: {err}")
        return True
    available = max_conn - reserved
    total = budget["total"]
    detail = (f"理论峰值 {total} = 单进程 {budget['per_worker']} (同步 {budget['sync']} "
              f"+ 异步池 {budget['async_pooled']} + 回退 {budget['async_fallback']}) "
              f"x worker {budget['workers']}，数据库可用 {available} "
              f"(max_connections {max_conn} - 保留 {reserved})")
    if total > available:
        logger.error(
            f"数据库连接额度不足：{detail}。"
            f"突发并发时可能出现 TooManyConnectionsError。"
            f"请调大 max_connections，或调小 API_WORKERS / DB_POSTGRESQL_MAX_OVERFLOW / "
            f"DB_ASYNC_MAX_OVERFLOW / DB_ASYNC_FALLBACK_LIMIT"
        )
        return False
    logger.info(f"数据库连接额度校验通过：{detail}")
    return True
