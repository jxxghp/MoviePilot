"""
数据库会话与异步连接池。

NullPool 之所以曾被硬编码，是因为它「永不复用」从而「永不跨事件循环」——asyncpg 的
Connection 与 aiosqlite 的线程都绑定在创建它的循环上。但它同时移除了唯一的背压：
每个异步会话独占一条物理连接且无上限。

这里按事件循环缓存带池引擎：常驻主循环走连接池（有上限、可复用），其余循环回退
全局 NullPool 引擎以保持跨循环安全，并由全局配额为其补上背压。
"""
import asyncio
import inspect
import threading
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator, Dict, Generator, Optional, Tuple, cast

from sqlalchemy.ext.asyncio import AsyncEngine as SaAsyncEngine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session, scoped_session, sessionmaker

import app.db.engine as engine_module
from app.db.engine import (_async_pool_enabled, _get_database_engine,
                           _database_backend_label, get_engine,
                           get_global_async_engine)
from app.runtime.config import global_vars, settings
from app.runtime.log import logger
from app.runtime.observability import record_metric

# 会话工厂同样惰性：sessionmaker 在构造时就要绑定引擎，模块级构造等于把引擎的
# 创建时机重新拉回 import 期，惰性化就白做了。
_factory_lock = threading.RLock()
_session_factory: Optional[sessionmaker] = None
_async_session_factory: Optional[async_sessionmaker] = None
_scoped_session: Optional[scoped_session] = None


def get_session_factory() -> sessionmaker:
    """
    获取同步会话工厂，首次调用时按需绑定引擎。
    :return: 同步会话工厂
    """
    global _session_factory
    if _session_factory is None:
        with _factory_lock:
            if _session_factory is None:
                _session_factory = sessionmaker(bind=get_engine())
    return _session_factory


def get_async_session_factory() -> async_sessionmaker:
    """
    获取异步会话工厂，首次调用时按需绑定全局异步引擎。
    :return: 异步会话工厂
    """
    global _async_session_factory
    if _async_session_factory is None:
        with _factory_lock:
            if _async_session_factory is None:
                _async_session_factory = async_sessionmaker(
                    bind=get_global_async_engine(), class_=AsyncSession)
    return _async_session_factory


def get_scoped_session() -> scoped_session:
    """
    获取线程局部的会话注册表，首次调用时创建。
    :return: scoped_session
    """
    global _scoped_session
    if _scoped_session is None:
        with _factory_lock:
            if _scoped_session is None:
                _scoped_session = scoped_session(get_session_factory())
    return _scoped_session


# SessionFactory / AsyncSessionFactory / ScopedSession 这三个旧名字保留为**转发函数**，
# 而不是模块级 __getattr__（PEP 562）。
#
# __getattr__ 只在「对模块对象取属性」时触发。`from app.db.session import ScopedSession`
# 确实会走到它，但那样每个导入方都会在 import 期把引擎创建出来，惰性化等于白做；而模块
# 自己的函数体里写裸名字 ScopedSession 则**根本不会**触发它——那是全局名字查找，只查模块
# __dict__ 和 builtins，直接 NameError。
#
# 做成 __dict__ 里真实存在的函数就两头都成立：导入它不碰引擎，调用它才创建。全仓库对这三个
# 名字的用法都是 `X()` 取一个会话，这一形式的语义与原先的 sessionmaker / scoped_session 实例
# 完全一致；patch("app.scheduler.SessionFactory", ...) 这类既有测试替身也照旧生效。
#
# 但仅限 `X()` 这一形式：它们不再是 sessionmaker / scoped_session 实例，因此实例上的其余接口
# （ScopedSession.remove()、SessionFactory.configure()、AsyncSessionFactory.begin()、
# scoped_session(SessionFactory) 等）不再可用。需要真正的工厂对象时用
# get_scoped_session() / get_session_factory() / get_async_session_factory()。
#
# 这三个名字已从 app/db/__init__.py 的 __all__ 中移除，属包内实现细节而非对外契约：它们建出
# 的是绕过事务装饰器的裸会话，提交/回滚/释放全得调用方自己兜底。包内的既有调用方（scheduler、
# postgresql 模块、Alembic 迁移脚本）走的是直接导入，不受 __all__ 影响。
def SessionFactory() -> Session:  # noqa: N802
    """
    创建一个同步会话，引擎在首次调用时才建立。
    :return: Session
    """
    return get_session_factory()()


def AsyncSessionFactory() -> AsyncSession:  # noqa: N802
    """
    创建一个异步会话，全局异步引擎在首次调用时才建立。
    :return: AsyncSession
    """
    return get_async_session_factory()()


def ScopedSession() -> Session:  # noqa: N802
    """
    取当前线程的会话，引擎在首次调用时才建立。
    :return: Session
    """
    return get_scoped_session()()


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


def _resolve_async_engine() -> Tuple[SaAsyncEngine, bool]:
    """
    按事件循环解析异步引擎，并一并给出它是否为池化引擎。

    「是否池化」必须由这里给出，不能让调用方拿 `engine is not get_global_async_engine()`
    反推：那个比较本身就会把全局引擎创建出来——池化路径压根用不到它，却因为一次身份比较
    多出一个从未使用的活引擎，而且第一个异步请求会在事件循环内部去抢引擎创建锁。
    :return: (异步引擎, 是否池化)
    """
    loop = _pooled_loop()
    if loop is None:
        return get_global_async_engine(), False
    key = id(loop)
    engine = _pooled_async_engines.get(key)
    if engine is not None:
        return engine, True
    with _pooled_async_lock:
        engine = _pooled_async_engines.get(key)
        if engine is None:
            engine = cast(SaAsyncEngine, _get_database_engine(is_async=True, pooled=True))
            _pooled_async_engines[key] = engine
            logger.info(f"异步数据库连接池已启用: pool_size={settings.DB_ASYNC_POOL_SIZE}, "
                        f"max_overflow={settings.DB_ASYNC_MAX_OVERFLOW}")
    return engine, True


def get_async_engine() -> SaAsyncEngine:
    """
    按事件循环获取异步引擎：常驻主循环用池化引擎，其余回退全局 NullPool 引擎。
    :return: 异步引擎
    """
    return _resolve_async_engine()[0]


async def _acquire_fallback_slot():
    """
    为回退路径申请一个全局连接配额。

    池化路径由连接池自身限流；回退路径若不加约束，临时循环上的突发并发会重新
    变得无界。信号量是线程安全且与事件循环无关的，但不能在协程里阻塞获取，
    因此用非阻塞获取 + 异步让出。
    """
    started_at = time.monotonic()
    deadline = started_at + settings.DB_POOL_TIMEOUT
    outcome = "success"
    try:
        while not _fallback_slots.acquire(blocking=False):
            if time.monotonic() >= deadline:
                outcome = "timeout"
                record_metric(
                    "db.pool.timeout",
                    backend=_database_backend_label(),
                )
                raise TimeoutError(
                    f"异步数据库连接配额已耗尽（上限 {settings.DB_ASYNC_FALLBACK_LIMIT}），"
                    f"等待超过 {settings.DB_POOL_TIMEOUT} 秒"
                )
            await asyncio.sleep(0.01)
    finally:
        record_metric(
            "db.pool.wait",
            time.monotonic() - started_at,
            backend=_database_backend_label(),
            outcome=outcome,
        )


@asynccontextmanager
async def async_session_scope() -> AsyncGenerator[AsyncSession, None]:
    """
    获取异步数据库会话。

    这是异步会话的唯一入口：池化与配额都在这里收口，调用方无需感知
    当前运行在哪个事件循环上。
    :return: AsyncSession
    """
    engine, pooled = _resolve_async_engine()
    if not pooled:
        await _acquire_fallback_slot()
    try:
        async with AsyncSession(bind=engine, expire_on_commit=False) as session:
            yield session
    finally:
        if not pooled:
            _fallback_slots.release()


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


async def _dispose_engine(engine: Any, label: str) -> None:
    """
    释放单个引擎，异常只打印不上抛，避免拖累其余引擎的释放。

    同步引擎的 dispose 是普通函数、异步引擎的返回协程，这里一并处理，
    好让三类引擎共用同一套异常隔离。
    :param engine: 待释放的引擎
    :param label: 出错时用于定位是哪一个引擎
    """
    try:
        result = engine.dispose()
        if inspect.isawaitable(result):
            await result
    except Exception as err:  # noqa: BLE001
        print(f"Error while disposing {label}: {err}")


async def close_database():
    """
    关闭所有数据库连接并清理资源。

    逐个引擎隔离异常，而不是外面套一个大 try：套大 try 时同步引擎 dispose 一抛错，
    异步引擎与全部池化引擎就都跳过了释放——一条坏连接拖着其余连接一起泄漏，
    正是关停路径最不该出现的失败方式。
    """
    # 先释放全部插件数据库容器，再 dispose 宿主引擎：PostgreSQL 下插件引擎是宿主
    # 引擎的外观，反过来会让插件会话清理落在已失效的连接上。函数内 import 避免
    # app.db.plugin 经 app.plugins 拉起的一整条依赖链在包内成环。
    from app.db.plugin import release_all as _release_plugin_databases
    _release_plugin_databases()
    # 只释放确实创建过的引擎：惰性之后，为了 dispose 而先把引擎创建出来毫无意义，
    # 还会在从未用过数据库的进程里凭空连一次库
    sync_engine = engine_module.peek_sync_engine()
    if sync_engine is not None:
        await _dispose_engine(sync_engine, "sync engine")
    async_engine = engine_module.peek_async_engine()
    if async_engine is not None:
        await _dispose_engine(async_engine, "global async engine")
    # 释放按事件循环缓存的池化引擎
    with _pooled_async_lock:
        engines = list(_pooled_async_engines.values())
        _pooled_async_engines.clear()
    for engine in engines:
        await _dispose_engine(engine, "pooled async engine")
