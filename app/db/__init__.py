import asyncio
import threading
import time
from contextlib import asynccontextmanager
from typing import Any, Dict, Generator, List, Optional, Self, Tuple, AsyncGenerator, Union

from sqlalchemy import NullPool, QueuePool, and_, create_engine, event, inspect, text, select, delete, Column, Integer, \
    Sequence, Identity
from sqlalchemy.engine import Engine as SQLAlchemyEngine, ExceptionContext
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session, as_declarative, declared_attr, scoped_session, sessionmaker

from app.runtime.config import global_vars, settings
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


def get_id_column():
    """
    根据数据库类型返回合适的ID列定义
    """
    if settings.DB_TYPE.lower() == "postgresql":
        # PostgreSQL使用SERIAL类型，让数据库自动处理序列
        return Column(Integer, Identity(start=1, cycle=True), primary_key=True)
    else:
        # SQLite使用Sequence
        return Column(Integer, Sequence('id'), primary_key=True)


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
            "url": f"sqlite:///{settings.CONFIG_PATH}/user.db",
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
            "url": f"sqlite+aiosqlite:///{settings.CONFIG_PATH}/user.db",
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
Engine = _get_database_engine(is_async=False)

# 异步数据库引擎
AsyncEngine = _get_database_engine(is_async=True)

# 同步会话工厂
SessionFactory = sessionmaker(bind=Engine)

# 异步会话工厂
AsyncSessionFactory = async_sessionmaker(bind=AsyncEngine, class_=AsyncSession)

# ==================== 异步引擎的按事件循环池化 ====================
# NullPool 之所以被选用，是因为它「永不复用」从而「永不跨事件循环」——asyncpg 的
# Connection 与 aiosqlite 的线程都绑定在创建它的循环上，跨循环复用会抛出
# "Task got Future attached to a different loop"。
#
# 但它同时也移除了唯一的背压：每个异步会话独占一条物理连接且无上限。调度器以上百个
# 线程向主循环投递协程，突发并发会直接顶穿 PostgreSQL 的 max_connections；SQLite 侧
# 则表现为 WAL 写争用与反复 checkpoint 造成的长时间卡顿。
#
# 这里按事件循环缓存带池引擎：常驻主循环走连接池（有上限、可复用），其余循环回退
# 全局 NullPool 引擎以保持跨循环安全，并由 _fallback_slots 为其补上背压。
_pooled_async_engines: Dict[int, Any] = {}
_pooled_async_lock = threading.Lock()
# 回退路径（未池化的临时循环）共享的全局连接配额。用 threading 信号量而非
# asyncio.Semaphore：后者绑定单个事件循环，无法跨循环生效
_fallback_slots = threading.BoundedSemaphore(max(1, settings.DB_ASYNC_FALLBACK_LIMIT))


def _async_pool_enabled() -> bool:
    """
    是否启用异步连接池。设为 NullPool 可回退到池化前的行为。
    """
    return str(settings.DB_ASYNC_POOL_TYPE or "").strip().lower() != "nullpool"


def _pooled_loop() -> Optional[Any]:
    """
    取当前可池化的事件循环，不可池化时返回 None。

    只认常驻主循环：它承载了绝大多数异步 DB 流量，且生命周期与进程一致，
    池中连接不会因循环销毁而失效。
    直接读 CURRENT_EVENT_LOOP 而不用 global_vars.loop——后者在未设置时会
    新建一个事件循环，仅为判断就产生副作用是不可接受的。
    """
    if not _async_pool_enabled():
        return None
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # 没有运行中的循环，行为与池化前一致
        return None
    if loop is not getattr(global_vars, "CURRENT_EVENT_LOOP", None):
        return None
    return loop


def get_async_engine():
    """
    按事件循环获取异步引擎：常驻主循环用池化引擎，其余回退全局 NullPool 引擎。
    :return: 异步引擎
    """
    loop = _pooled_loop()
    if loop is None:
        return AsyncEngine
    key = id(loop)
    engine = _pooled_async_engines.get(key)
    if engine is not None:
        return engine
    with _pooled_async_lock:
        engine = _pooled_async_engines.get(key)
        if engine is None:
            engine = _get_database_engine(is_async=True, pooled=True)
            _pooled_async_engines[key] = engine
            logger.info(f"异步数据库连接池已启用: pool_size={settings.DB_ASYNC_POOL_SIZE}, "
                        f"max_overflow={settings.DB_ASYNC_MAX_OVERFLOW}")
    return engine


async def _acquire_fallback_slot():
    """
    为回退路径申请一个全局连接配额。

    池化路径由连接池自身限流；回退路径若不加约束，临时循环上的突发并发会重新
    变得无界。信号量是线程安全且与事件循环无关的，但不能在协程里阻塞获取，
    因此用非阻塞获取 + 异步让出。
    """
    deadline = time.monotonic() + settings.DB_POOL_TIMEOUT
    while not _fallback_slots.acquire(blocking=False):
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"异步数据库连接配额已耗尽（上限 {settings.DB_ASYNC_FALLBACK_LIMIT}），"
                f"等待超过 {settings.DB_POOL_TIMEOUT} 秒"
            )
        await asyncio.sleep(0.01)


@asynccontextmanager
async def async_session_scope() -> AsyncGenerator[AsyncSession, None]:
    """
    获取异步数据库会话。

    这是异步会话的唯一入口：池化与配额都在这里收口，调用方无需感知
    当前运行在哪个事件循环上。
    :return: AsyncSession
    """
    engine = get_async_engine()
    pooled = engine is not AsyncEngine
    if not pooled:
        await _acquire_fallback_slot()
    try:
        async with AsyncSession(bind=engine, expire_on_commit=False) as session:
            yield session
    finally:
        if not pooled:
            _fallback_slots.release()


# 同步多线程全局使用的数据库会话
ScopedSession = scoped_session(SessionFactory)


def get_db() -> Generator:
    """
    获取数据库会话，用于WEB请求
    :return: Session
    """
    db = None
    try:
        db = SessionFactory()
        yield db
    finally:
        if db:
            db.close()


async def get_async_db() -> AsyncGenerator[AsyncSession, None]:
    """
    获取异步数据库会话，用于WEB请求
    :return: AsyncSession
    """
    async with async_session_scope() as session:
        yield session


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
            max_conn = int(conn.execute(text("SHOW max_connections")).scalar())
            reserved = int(conn.execute(text("SHOW superuser_reserved_connections")).scalar())
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


async def close_database():
    """
    关闭所有数据库连接并清理资源
    """
    try:
        # 释放同步连接池
        Engine.dispose()  # noqa
        # 释放异步连接池
        await AsyncEngine.dispose()
        # 释放按事件循环缓存的池化引擎
        with _pooled_async_lock:
            engines = list(_pooled_async_engines.values())
            _pooled_async_engines.clear()
        for engine in engines:
            try:
                await engine.dispose()
            except Exception as err:  # noqa: BLE001
                print(f"Error while disposing pooled async engine: {err}")
    except Exception as err:
        print(f"Error while disposing database connections: {err}")


def _get_args_db(args: tuple, kwargs: dict) -> Optional[Session]:
    """
    从参数中获取数据库Session对象
    """
    db = None
    if args:
        for arg in args:
            if isinstance(arg, Session):
                db = arg
                break
    if kwargs:
        for key, value in kwargs.items():
            if isinstance(value, Session):
                db = value
                break
    return db


def _get_args_async_db(args: tuple, kwargs: dict) -> Optional[AsyncSession]:
    """
    从参数中获取异步数据库AsyncSession对象
    """
    db = None
    if args:
        for arg in args:
            if isinstance(arg, AsyncSession):
                db = arg
                break
    if kwargs:
        for key, value in kwargs.items():
            if isinstance(value, AsyncSession):
                db = value
                break
    return db


def _update_args_db(args: tuple, kwargs: dict, db: Session) -> Tuple[tuple, dict]:
    """
    更新参数中的数据库Session对象，关键字传参时更新db的值，否则更新第1或第2个参数
    """
    if kwargs and 'db' in kwargs:
        kwargs['db'] = db
    elif args:
        if args[0] is None:
            args = (db, *args[1:])
        else:
            args = (args[0], db, *args[2:])
    return args, kwargs


def _update_args_async_db(args: tuple, kwargs: dict, db: AsyncSession) -> Tuple[tuple, dict]:
    """
    更新参数中的异步数据库AsyncSession对象，关键字传参时更新db的值，否则更新第1或第2个参数
    """
    if kwargs and 'db' in kwargs:
        kwargs['db'] = db
    elif args:
        if args[0] is None:
            args = (db, *args[1:])
        else:
            args = (args[0], db, *args[2:])
    return args, kwargs


def db_update(func):
    """
    数据库更新类操作装饰器，第一个参数必须是数据库会话或存在db参数
    """

    def wrapper(*args, **kwargs):
        # 是否关闭数据库会话
        _close_db = False
        # 从参数中获取数据库会话
        db = _get_args_db(args, kwargs)
        if not db:
            # 如果没有获取到数据库会话，创建一个
            db = ScopedSession()
            # 标记需要关闭数据库会话
            _close_db = True
            # 更新参数中的数据库会话
            args, kwargs = _update_args_db(args, kwargs, db)
        try:
            # 执行函数
            result = func(*args, **kwargs)
            # 提交事务
            db.commit()
        except Exception as err:
            # 回滚事务
            db.rollback()
            raise err
        finally:
            # 关闭数据库会话
            if _close_db:
                db.close()
        return result

    return wrapper


def async_db_update(func):
    """
    异步数据库更新类操作装饰器，第一个参数必须是异步数据库会话或存在db参数
    """

    async def wrapper(*args, **kwargs):
        # 是否关闭数据库会话
        _close_db = False
        # 从参数中获取异步数据库会话
        db = _get_args_async_db(args, kwargs)
        if not db:
            # 如果没有获取到异步数据库会话，创建一个。经 async_session_scope
            # 统一收口：常驻主循环走连接池，其余循环走 NullPool 并占用全局配额
            _scope = async_session_scope()
            db = await _scope.__aenter__()
            # 标记需要关闭数据库会话
            _close_db = True
            # 更新参数中的异步数据库会话
            args, kwargs = _update_args_async_db(args, kwargs, db)
        try:
            # 执行函数
            result = await func(*args, **kwargs)
            # 提交事务
            await db.commit()
        except Exception as err:
            # 回滚事务
            await db.rollback()
            raise err
        finally:
            # 关闭数据库会话
            if _close_db:
                # 退出会话上下文而不是只 close：配额的释放绑定在 __aexit__ 上，
                # 只关会话会让回退路径的全局配额永不归还，最终把自己饿死
                await _scope.__aexit__(None, None, None)
        return result

    return wrapper


def db_query(func):
    """
    数据库查询操作装饰器，第一个参数必须是数据库会话或存在db参数
    注意：db.query列表数据时，需要转换为list返回
    """

    def wrapper(*args, **kwargs):
        # 是否关闭数据库会话
        _close_db = False
        # 从参数中获取数据库会话
        db = _get_args_db(args, kwargs)
        if not db:
            # 如果没有获取到数据库会话，创建一个
            db = ScopedSession()
            # 标记需要关闭数据库会话
            _close_db = True
            # 更新参数中的数据库会话
            args, kwargs = _update_args_db(args, kwargs, db)
        try:
            # 执行函数
            result = func(*args, **kwargs)
        except Exception as err:
            raise err
        finally:
            # 关闭数据库会话
            if _close_db:
                db.close()
        return result

    return wrapper


def async_db_query(func):
    """
    异步数据库查询操作装饰器，第一个参数必须是异步数据库会话或存在db参数
    注意：db.query列表数据时，需要转换为list返回
    """

    async def wrapper(*args, **kwargs):
        # 是否关闭数据库会话
        _close_db = False
        # 从参数中获取异步数据库会话
        db = _get_args_async_db(args, kwargs)
        if not db:
            # 如果没有获取到异步数据库会话，创建一个。经 async_session_scope
            # 统一收口：常驻主循环走连接池，其余循环走 NullPool 并占用全局配额
            _scope = async_session_scope()
            db = await _scope.__aenter__()
            # 标记需要关闭数据库会话
            _close_db = True
            # 更新参数中的异步数据库会话
            args, kwargs = _update_args_async_db(args, kwargs, db)
        try:
            # 执行函数
            result = await func(*args, **kwargs)
        except Exception as err:
            raise err
        finally:
            # 关闭数据库会话
            if _close_db:
                # 退出会话上下文而不是只 close：配额的释放绑定在 __aexit__ 上，
                # 只关会话会让回退路径的全局配额永不归还，最终把自己饿死
                await _scope.__aexit__(None, None, None)
        return result

    return wrapper


@as_declarative()
class Base:
    id: Any
    __name__: str

    @db_update
    def create(self, db: Session):
        db.add(self)

    @async_db_update
    async def async_create(self, db: AsyncSession):
        db.add(self)
        await db.flush()
        return self

    @classmethod
    @db_query
    def get(cls, db: Session, rid: int) -> Self:
        return db.query(cls).filter(and_(cls.id == rid)).first()

    @classmethod
    @async_db_query
    async def async_get(cls, db: AsyncSession, rid: int) -> Self:
        result = await db.execute(select(cls).where(and_(cls.id == rid)))
        return result.scalars().first()

    @db_update
    def update(self, db: Session, payload: dict):
        for key, value in payload.items():
            setattr(self, key, value)
        if inspect(self).detached:
            db.add(self)

    @async_db_update
    async def async_update(self, db: AsyncSession, payload: dict):
        for key, value in payload.items():
            setattr(self, key, value)
        if inspect(self).detached:
            db.add(self)

    @classmethod
    @db_update
    def delete(cls, db: Session, rid):
        db.query(cls).filter(and_(cls.id == rid)).delete()

    @classmethod
    @async_db_update
    async def async_delete(cls, db: AsyncSession, rid):
        result = await db.execute(select(cls).where(and_(cls.id == rid)))
        user = result.scalars().first()
        if user:
            await db.delete(user)

    @classmethod
    @db_update
    def truncate(cls, db: Session):
        db.query(cls).delete()

    @classmethod
    @async_db_update
    async def async_truncate(cls, db: AsyncSession):
        await db.execute(delete(cls))

    @classmethod
    @db_query
    def list(cls, db: Session) -> List[Self]:
        return db.query(cls).all()

    @classmethod
    @async_db_query
    async def async_list(cls, db: AsyncSession) -> Sequence[Self]:
        result = await db.execute(select(cls))
        return result.scalars().all()

    def to_dict(self):
        return {c.name: getattr(self, c.name, None) for c in self.__table__.columns}  # noqa

    @declared_attr
    def __tablename__(self) -> str:
        return self.__name__.lower()


class DbOper:
    """
    数据库操作基类
    """

    def __init__(self, db: Union[Session, AsyncSession] = None):
        self._db = db
