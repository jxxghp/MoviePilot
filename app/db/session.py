"""
数据库会话与异步连接池。

NullPool 之所以曾被硬编码，是因为它「永不复用」从而「永不跨事件循环」——asyncpg 的
Connection 与 aiosqlite 的线程都绑定在创建它的循环上。但它同时移除了唯一的背压：
每个异步会话独占一条物理连接且无上限。

这里按事件循环缓存带池引擎：常驻主循环走连接池（有上限、可复用），其余循环回退
全局 NullPool 引擎以保持跨循环安全，并由全局配额为其补上背压。
"""
import asyncio
import threading
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator, Dict, Generator, Optional, cast

from sqlalchemy.ext.asyncio import AsyncEngine as SaAsyncEngine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import scoped_session, sessionmaker

from app.runtime.config import global_vars, settings
from app.db.engine import AsyncEngine, Engine, _async_pool_enabled, _get_database_engine
from app.runtime.log import logger

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


def get_async_engine() -> SaAsyncEngine:
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
            engine = cast(SaAsyncEngine, _get_database_engine(is_async=True, pooled=True))
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
