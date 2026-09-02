"""
数据库引擎的构建与连接额度核算。

同步引擎与未池化的全局异步引擎都在此按需创建（首次访问时，不在 import 期）；
按事件循环池化的异步引擎由 session 模块创建。三者的构建参数在这里收口。
"""

import threading
from typing import Any, Dict, Optional, cast

from sqlalchemy import NullPool, QueuePool, create_engine, event, text
from sqlalchemy.engine import Engine as SyncEngine
from sqlalchemy.ext.asyncio import AsyncEngine as SaAsyncEngine
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import Pool

from app.db.diagnostics import _register_database_error_logging
from app.db.worker import DATABASE_WORKER_MAX_WORKERS
from app.foundation.environment import is_free_threaded_runtime
from app.runtime.log import logger
from app.runtime.observability import record_metric
from app.runtime.settings import get_runtime_setting


def _database_backend_label() -> str:
    """把数据库类型收敛为有限的观测标签。"""
    return "postgresql" if get_runtime_setting("DB_TYPE").lower() == "postgresql" else "sqlite"


def _sync_postgresql_driver() -> Optional[str]:
    """free-threaded 解释器使用不会重新启用 GIL 的 psycopg 驱动。"""
    return "psycopg" if is_free_threaded_runtime() else None


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


def _register_sqlite_foreign_keys(engine: SyncEngine) -> None:
    """为每条 SQLite 连接启用模型声明的级联和引用完整性约束。"""

    def enable_foreign_keys(
        dbapi_connection: Any,
        _connection_record: Any,
    ) -> None:
        """在连接进入池前启用 SQLite 外键检查。"""
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()

    event.listen(engine, "connect", enable_foreign_keys)


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
        "pool_size": get_runtime_setting("DB_ASYNC_POOL_SIZE"),
        "max_overflow": get_runtime_setting("DB_ASYNC_MAX_OVERFLOW"),
        "pool_timeout": get_runtime_setting("DB_POOL_TIMEOUT"),
    }


def apply_sqlite_journal_mode(engine: SyncEngine) -> Any:
    """
    把配置声明的 journal_mode 写入引擎所指的库文件，并返回实际生效值。

    journal_mode 是库文件级的持久属性，每个库文件都要有人设置一次。宿主库与插件自有库
    共用这一段，不会出现「宿主是 WAL、插件库是 DELETE」的分裂行为。
    :param engine: 同步引擎
    :return: 生效的 journal_mode
    """
    journal_mode = "WAL" if get_runtime_setting("DB_WAL_ENABLE") else "DELETE"
    with engine.connect() as connection:
        return connection.execute(text(f"PRAGMA journal_mode={journal_mode};")).scalar()


def build_sqlite_engine(url: str) -> SyncEngine:
    """
    按宿主 SQLite 连接策略构建指向给定库文件的同步引擎。

    连接超时、驱动级参数、连接池、外键约束、错误日志与池指标在这里收口，宿主库与插件
    自有库因此共享同一套连接语义，不会因为落在不同文件而出现两种行为。
    :param url: SQLite 连接串
    :return: 同步引擎
    """
    # 连接参数
    _connect_args = {
        "timeout": get_runtime_setting("DB_TIMEOUT"),
    }
    # 允许部署侧注入驱动级参数（如 PgBouncer 事务模式下的 statement_cache_size）
    _connect_args.update(get_runtime_setting("DB_CONNECT_ARGS") or {})
    # 启用 WAL 模式时的额外配置
    if get_runtime_setting("DB_WAL_ENABLE"):
        _connect_args["check_same_thread"] = False

    # 根据池类型设置 poolclass 和相关参数
    _pool_class = NullPool if get_runtime_setting("DB_POOL_TYPE") == "NullPool" else QueuePool

    # 数据库参数
    _db_kwargs = {
        "url": url,
        "pool_pre_ping": get_runtime_setting("DB_POOL_PRE_PING"),
        "echo": get_runtime_setting("DB_ECHO"),
        "poolclass": _pool_class,
        "pool_recycle": get_runtime_setting("DB_POOL_RECYCLE"),
        "connect_args": _connect_args,
    }

    # 当使用 QueuePool 时，添加 QueuePool 特有的参数
    if _pool_class == QueuePool:
        _db_kwargs.update(
            {
                "pool_size": get_runtime_setting("DB_SQLITE_POOL_SIZE"),
                "pool_timeout": get_runtime_setting("DB_POOL_TIMEOUT"),
                "max_overflow": get_runtime_setting("DB_SQLITE_MAX_OVERFLOW"),
            }
        )

    engine = create_engine(**_db_kwargs)
    _register_sqlite_foreign_keys(engine)
    _register_database_error_logging(engine)
    _register_database_pool_metrics(engine)
    return engine


def _get_database_engine(is_async: bool = False, pooled: bool = False):
    """
    获取数据库连接参数并设置WAL模式
    :param is_async: 是否创建异步引擎，True - 异步引擎, False - 同步引擎
    :param pooled: 异步引擎是否启用连接池，仅对常驻事件循环使用
    :return: 返回对应的数据库引擎
    """
    # 根据数据库类型选择连接方式
    if get_runtime_setting("DB_TYPE").lower() == "postgresql":
        return _get_postgresql_engine(is_async, pooled=pooled)
    else:
        return _get_sqlite_engine(is_async, pooled=pooled)


def _get_sqlite_engine(is_async: bool = False, pooled: bool = False):
    """
    获取SQLite数据库引擎
    """
    # 创建同步引擎
    if not is_async:
        engine = build_sqlite_engine(get_runtime_setting("DB_SQLITE_URL")())

        # 设置WAL模式。
        # 这是引擎构建里唯一的阻塞 I/O，且发生在 get_engine() 的创建锁内——异步侧因此
        # 移除了对称的那一段（见下方 else 分支）。同步侧保留是因为 journal_mode 必须有人
        # 设置一次，而同步引擎的首次创建由 lifespan 数据库准备组件中的 init_db() 完成，
        # 不存在一群线程
        # 等在锁上的场面；即便退化到运行期首次访问，阻塞的也只是本地 SQLite 的一次 PRAGMA。
        print(f"SQLite database journal mode set to: {apply_sqlite_journal_mode(engine)}")

        return engine
    else:
        # 连接参数
        _connect_args = {
            "timeout": get_runtime_setting("DB_TIMEOUT"),
        }
        # 允许部署侧注入驱动级参数（如 PgBouncer 事务模式下的 statement_cache_size）
        _connect_args.update(get_runtime_setting("DB_CONNECT_ARGS") or {})
        # 启用 WAL 模式时的额外配置
        if get_runtime_setting("DB_WAL_ENABLE"):
            _connect_args["check_same_thread"] = False

        # 数据库参数，只能使用 NullPool
        _db_kwargs = {
            "url": get_runtime_setting("DB_SQLITE_URL")("aiosqlite"),
            "pool_pre_ping": get_runtime_setting("DB_POOL_PRE_PING"),
            "echo": get_runtime_setting("DB_ECHO"),
            "pool_recycle": get_runtime_setting("DB_POOL_RECYCLE"),
            "connect_args": _connect_args,
            **_async_pool_kwargs(pooled),
        }
        # 创建异步数据库引擎
        async_engine = create_async_engine(**_db_kwargs)
        _register_sqlite_foreign_keys(async_engine.sync_engine)
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
    db_url = get_runtime_setting("DB_POSTGRESQL_URL")(_sync_postgresql_driver())

    # PostgreSQL连接参数。允许部署侧注入驱动级参数，
    # 例如经 PgBouncer 事务模式接入时 asyncpg 需要 statement_cache_size=0
    _connect_args = dict(get_runtime_setting("DB_CONNECT_ARGS") or {})

    # 创建同步引擎
    if not is_async:
        # 根据池类型设置 poolclass 和相关参数
        _pool_class = NullPool if get_runtime_setting("DB_POOL_TYPE") == "NullPool" else QueuePool

        # 数据库参数
        _db_kwargs = {
            "url": db_url,
            "pool_pre_ping": get_runtime_setting("DB_POOL_PRE_PING"),
            "echo": get_runtime_setting("DB_ECHO"),
            "poolclass": _pool_class,
            "pool_recycle": get_runtime_setting("DB_POOL_RECYCLE"),
            "connect_args": _connect_args,
        }

        # 当使用 QueuePool 时，添加 QueuePool 特有的参数
        if _pool_class == QueuePool:
            _db_kwargs.update(
                {
                    "pool_size": get_runtime_setting("DB_POSTGRESQL_POOL_SIZE"),
                    "pool_timeout": get_runtime_setting("DB_POOL_TIMEOUT"),
                    "max_overflow": get_runtime_setting("DB_POSTGRESQL_MAX_OVERFLOW"),
                }
            )

        # 创建数据库引擎
        engine = create_engine(**_db_kwargs)
        _register_database_error_logging(engine)
        _register_database_pool_metrics(engine)
        print(
            f"PostgreSQL database connected to {get_runtime_setting('DB_POSTGRESQL_TARGET')}/{get_runtime_setting('DB_POSTGRESQL_DATABASE')}"
        )

        return engine
    else:
        async_db_url = get_runtime_setting("DB_POSTGRESQL_URL")("asyncpg")

        # 数据库参数，只能使用 NullPool
        _db_kwargs = {
            "url": async_db_url,
            "pool_pre_ping": get_runtime_setting("DB_POOL_PRE_PING"),
            "echo": get_runtime_setting("DB_ECHO"),
            "pool_recycle": get_runtime_setting("DB_POOL_RECYCLE"),
            "connect_args": _connect_args,
            **_async_pool_kwargs(pooled),
        }
        # 创建异步数据库引擎
        async_engine = create_async_engine(**_db_kwargs)
        _register_database_error_logging(async_engine.sync_engine)
        _register_database_pool_metrics(async_engine.sync_engine)
        print(
            f"Async PostgreSQL database connected to {get_runtime_setting('DB_POSTGRESQL_TARGET')}/{get_runtime_setting('DB_POSTGRESQL_DATABASE')}"
        )

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
    return str(get_runtime_setting("DB_ASYNC_POOL_TYPE") or "").strip().lower() != "nullpool"


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
    if get_runtime_setting("DB_TYPE").lower() == "postgresql":
        sync_max = get_runtime_setting("DB_POSTGRESQL_POOL_SIZE") + get_runtime_setting("DB_POSTGRESQL_MAX_OVERFLOW")
    else:
        sync_max = get_runtime_setting("DB_SQLITE_POOL_SIZE") + get_runtime_setting("DB_SQLITE_MAX_OVERFLOW")
    if get_runtime_setting("DB_POOL_TYPE") == "NullPool":
        # 未池化连接由通用线程池和专属数据库 worker 共同创建，二者都要计入上限估计。
        sync_max = get_runtime_setting("CONF").threadpool + DATABASE_WORKER_MAX_WORKERS
    async_max = (
        get_runtime_setting("DB_ASYNC_POOL_SIZE") + get_runtime_setting("DB_ASYNC_MAX_OVERFLOW")
        if _async_pool_enabled()
        else 0
    )
    fallback = (
        get_runtime_setting("DB_ASYNC_FALLBACK_LIMIT")
        if _async_pool_enabled()
        else get_runtime_setting("CONF").scheduler
    )
    per_worker = sync_max + async_max + fallback
    # worker 数非法时按 1 计：退化成 0 会让合计归零、反而误判「额度充足」
    workers = get_runtime_setting("API_WORKERS", 1) or 1
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
    if get_runtime_setting("DB_TYPE").lower() != "postgresql":
        logger.info(
            f"数据库连接理论峰值: {budget['total']} "
            f"(单进程 {budget['per_worker']} = 同步 {budget['sync']} + 异步池 "
            f"{budget['async_pooled']} + 回退 {budget['async_fallback']}"
            f"，worker {budget['workers']})"
        )
        return True
    try:
        with get_engine().connect() as conn:
            max_conn = int(conn.execute(text("SHOW max_connections")).scalar() or 0)
            reserved = int(conn.execute(text("SHOW superuser_reserved_connections")).scalar() or 0)
    except Exception as err:
        logger.warn(f"无法读取 PostgreSQL 连接上限，跳过额度校验: {err}")
        return True
    available = max_conn - reserved
    total = budget["total"]
    detail = (
        f"理论峰值 {total} = 单进程 {budget['per_worker']} (同步 {budget['sync']} "
        f"+ 异步池 {budget['async_pooled']} + 回退 {budget['async_fallback']}) "
        f"x worker {budget['workers']}，数据库可用 {available} "
        f"(max_connections {max_conn} - 保留 {reserved})"
    )
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
