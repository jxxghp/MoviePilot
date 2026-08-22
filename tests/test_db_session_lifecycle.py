"""
数据库会话生命周期与资源释放测试。

会话生成器（FastAPI 依赖注入入口）必须在请求结束时归还连接，close_database 必须
释放全部引擎——池化之后引擎不再只有一个：除全局同步/异步引擎外，还有按事件循环
缓存的池化引擎，漏掉任何一类都是连接泄漏。
"""
import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.orm import scoped_session, sessionmaker

from app.runtime.config import global_vars, settings
from app.db import engine as engine_module
from app.db import session as session_module


@pytest.fixture(autouse=True)
def _restore_pooled_engines():
    """
    复原按循环缓存的引擎，避免用例间相互污染。
    """
    saved = dict(session_module._pooled_async_engines)
    yield
    session_module._pooled_async_engines.clear()
    session_module._pooled_async_engines.update(saved)


def test_get_db_closes_session_on_exit(monkeypatch):
    """
    同步会话生成器必须在迭代结束后关闭会话，否则连接不会归还连接池。
    """
    closed = []
    fake = MagicMock()
    fake.close = lambda: closed.append(1)
    monkeypatch.setattr(session_module, "SessionFactory", lambda: fake)

    gen = session_module.get_db()
    assert next(gen) is fake
    with pytest.raises(StopIteration):
        next(gen)

    assert closed, "生成器正常结束时未关闭会话"


def test_get_db_closes_session_even_on_error(monkeypatch):
    """
    调用方提前中止时同样要归还会话——否则一次请求失败就泄漏一条连接。
    """
    closed = []
    fake = MagicMock()
    fake.close = lambda: closed.append(1)
    monkeypatch.setattr(session_module, "SessionFactory", lambda: fake)

    gen = session_module.get_db()
    next(gen)
    gen.close()

    assert closed, "生成器被中止时未关闭会话"


def test_get_async_db_yields_session_from_scope(monkeypatch):
    """
    异步会话入口必须经 async_session_scope 获取——池化与配额都在那里收口，
    绕过它会同时失去连接复用和背压。
    """
    used = []

    class _Scope:
        """会话作用域替身，记录进入与退出。"""

        async def __aenter__(self):
            used.append("enter")
            return "SESSION"

        async def __aexit__(self, *_exc):
            used.append("exit")
            return False

    monkeypatch.setattr(session_module, "async_session_scope", lambda: _Scope())

    async def run():
        gen = session_module.get_async_db()
        got = await gen.__anext__()
        with pytest.raises(StopAsyncIteration):
            await gen.__anext__()
        return got

    assert asyncio.run(run()) == "SESSION"
    assert used == ["enter", "exit"], "会话作用域未正确进入/退出"


def test_scoped_sessions_are_not_shared_across_worker_threads(monkeypatch):
    """同步入口必须为并行工作线程提供不同 Session 实例。"""
    registry = scoped_session(sessionmaker())
    barrier = threading.Barrier(2)
    monkeypatch.setattr(session_module, "_scoped_session", registry)

    def open_in_thread() -> int:
        """在线程内持有会话直到另一个线程也完成解析。"""
        session = session_module.ScopedSession()
        try:
            barrier.wait(timeout=5)
            return id(session)
        finally:
            registry.remove()

    with ThreadPoolExecutor(max_workers=2) as executor:
        session_ids = list(executor.map(lambda _: open_in_thread(), range(2)))

    assert len(set(session_ids)) == 2


def test_async_session_scopes_are_not_shared_across_tasks(monkeypatch):
    """并发异步任务必须各自创建和关闭 AsyncSession 作用域。"""
    created: list[object] = []

    class FakeAsyncSession:
        """记录每次作用域构造的独立异步会话替身。"""

        def __init__(self, **_kwargs) -> None:
            """创建可由异步上下文管理器返回的唯一实例。"""
            created.append(self)

        async def __aenter__(self):
            """返回当前会话实例。"""
            return self

        async def __aexit__(self, *_exc) -> bool:
            """模拟正常释放且不吞掉异常。"""
            return False

    monkeypatch.setattr(
        session_module,
        "_resolve_async_engine",
        lambda: (object(), True),
    )
    monkeypatch.setattr(session_module, "AsyncSession", FakeAsyncSession)

    async def open_in_task() -> int:
        """进入一个任务私有的异步会话作用域。"""
        async with session_module.async_session_scope() as session:
            await asyncio.sleep(0)
            return id(session)

    async def run() -> list[int]:
        """并发执行两个会话作用域。"""
        return await asyncio.gather(open_in_task(), open_in_task())

    session_ids = asyncio.run(run())

    assert len(created) == 2
    assert len(set(session_ids)) == 2


def test_close_database_disposes_pooled_engines(monkeypatch):
    """
    close_database 必须释放按事件循环缓存的池化引擎。

    池化之后引擎不再只有全局那一个，漏掉缓存中的引擎意味着进程退出时
    仍持有未归还的物理连接。
    """
    sync_engine = MagicMock()
    async_engine = MagicMock(dispose=AsyncMock())
    pooled_a = MagicMock(dispose=AsyncMock())
    pooled_b = MagicMock(dispose=AsyncMock())

    monkeypatch.setattr(engine_module, "_sync_engine", sync_engine)
    monkeypatch.setattr(engine_module, "_async_engine", async_engine)
    session_module._pooled_async_engines.clear()
    session_module._pooled_async_engines.update({1: pooled_a, 2: pooled_b})

    asyncio.run(session_module.close_database())

    sync_engine.dispose.assert_called_once()
    async_engine.dispose.assert_awaited_once()
    pooled_a.dispose.assert_awaited_once()
    pooled_b.dispose.assert_awaited_once()
    assert not session_module._pooled_async_engines, "释放后未清空缓存"


def test_close_database_does_not_create_engines_to_dispose_them(monkeypatch):
    """
    两个引擎槽都是空的时候，close_database 不得为了 dispose 而把引擎创建出来。

    这是惰性化的直接后果，也是最容易在重构中丢掉的一条：写成 `Engine.dispose()`
    同样能跑通上面那几个用例——它们都把 MagicMock 塞进了引擎槽，`is not None` 恒真，
    于是「先创建再释放」和「有才释放」在测试里完全等价。因此必须单独用空槽压一次：
    否则一个从未用过数据库的进程会在关停时凭空连一次库，只为了随后释放它。
    """
    created = []
    monkeypatch.setattr(engine_module, "_sync_engine", None)
    monkeypatch.setattr(engine_module, "_async_engine", None)
    monkeypatch.setattr(engine_module, "_get_database_engine",
                        lambda **kw: created.append(kw) or MagicMock(dispose=AsyncMock()))
    session_module._pooled_async_engines.clear()

    asyncio.run(session_module.close_database())

    assert created == [], f"close_database 为了 dispose 创建了引擎：{created}"
    assert engine_module._sync_engine is None, "同步引擎槽被 close_database 填上了"
    assert engine_module._async_engine is None, "异步引擎槽被 close_database 填上了"


def test_close_database_continues_after_single_engine_failure(monkeypatch):
    """
    单个引擎释放失败不能中断其余引擎的释放，否则一个坏连接会让其他连接全部泄漏。
    """
    failing = MagicMock(dispose=AsyncMock(side_effect=RuntimeError("connection reset")))
    healthy = MagicMock(dispose=AsyncMock())

    monkeypatch.setattr(engine_module, "_sync_engine", MagicMock())
    monkeypatch.setattr(engine_module, "_async_engine", MagicMock(dispose=AsyncMock()))
    session_module._pooled_async_engines.clear()
    session_module._pooled_async_engines.update({1: failing, 2: healthy})

    asyncio.run(session_module.close_database())

    healthy.dispose.assert_awaited_once()


@pytest.mark.parametrize("failing", ["sync", "async"])
def test_close_database_releases_remaining_engines_after_global_failure(monkeypatch, failing):
    """
    全局引擎释放失败：既不能抛出，也不能连累后面的引擎。

    「不抛」是因为 close_database 在关闭流程末尾调用，抛异常会掩盖其他关闭步骤的问题。
    但只断言「不抛」是不够的——在外面套一个大 try 同样不抛，代价是同步引擎一出错，
    异步引擎和全部池化引擎就都跳过了释放：一条坏连接拖着其余连接一起泄漏，
    而这恰恰是兄弟用例 test_close_database_continues_after_single_engine_failure
    的 docstring 已经声称过的不变量。所以这里把它真正钉住：坏的那个失败，其余照常释放。
    """
    sync_engine = MagicMock()
    async_engine = MagicMock(dispose=AsyncMock())
    pooled = MagicMock(dispose=AsyncMock())
    if failing == "sync":
        sync_engine.dispose.side_effect = RuntimeError("boom")
    else:
        async_engine.dispose.side_effect = RuntimeError("boom")

    monkeypatch.setattr(engine_module, "_sync_engine", sync_engine)
    monkeypatch.setattr(engine_module, "_async_engine", async_engine)
    session_module._pooled_async_engines.clear()
    session_module._pooled_async_engines.update({1: pooled})

    asyncio.run(session_module.close_database())  # 不抛异常

    # 出错的那个也得真被尝试过，排在它后面的一个都不能少
    sync_engine.dispose.assert_called_once()
    async_engine.dispose.assert_awaited_once()
    pooled.dispose.assert_awaited_once()
    assert not session_module._pooled_async_engines, "释放后未清空缓存"


def test_pooled_engine_is_reused_within_same_loop(monkeypatch):
    """
    同一事件循环内必须复用同一个池化引擎实例。

    每次新建引擎等于每次新建一个连接池，连接无法复用，池化就退化回了 NullPool
    的行为——只是多了一层包装。
    """
    monkeypatch.setattr(settings, "DB_ASYNC_POOL_TYPE", "QueuePool", raising=False)
    created = []
    monkeypatch.setattr(session_module, "_get_database_engine",
                        lambda **kw: created.append(kw) or MagicMock())

    async def run():
        global_vars.CURRENT_EVENT_LOOP = asyncio.get_running_loop()
        session_module._pooled_async_engines.clear()
        first = session_module.get_async_engine()
        second = session_module.get_async_engine()
        return first is second

    saved = global_vars.CURRENT_EVENT_LOOP
    try:
        assert asyncio.run(run()) is True
        assert len(created) == 1, f"引擎被重复创建 {len(created)} 次"
        assert created[0]["pooled"] is True
    finally:
        global_vars.CURRENT_EVENT_LOOP = saved
