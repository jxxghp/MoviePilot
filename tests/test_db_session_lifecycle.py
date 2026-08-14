"""
数据库会话生命周期与资源释放测试。

会话生成器（FastAPI 依赖注入入口）必须在请求结束时归还连接，close_database 必须
释放全部引擎——池化之后引擎不再只有一个：除全局同步/异步引擎外，还有按事件循环
缓存的池化引擎，漏掉任何一类都是连接泄漏。
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.runtime.config import global_vars, settings
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

    monkeypatch.setattr(session_module, "Engine", sync_engine)
    monkeypatch.setattr(session_module, "AsyncEngine", async_engine)
    session_module._pooled_async_engines.clear()
    session_module._pooled_async_engines.update({1: pooled_a, 2: pooled_b})

    asyncio.run(session_module.close_database())

    sync_engine.dispose.assert_called_once()
    async_engine.dispose.assert_awaited_once()
    pooled_a.dispose.assert_awaited_once()
    pooled_b.dispose.assert_awaited_once()
    assert not session_module._pooled_async_engines, "释放后未清空缓存"


def test_close_database_continues_after_single_engine_failure(monkeypatch):
    """
    单个引擎释放失败不能中断其余引擎的释放，否则一个坏连接会让其他连接全部泄漏。
    """
    failing = MagicMock(dispose=AsyncMock(side_effect=RuntimeError("connection reset")))
    healthy = MagicMock(dispose=AsyncMock())

    monkeypatch.setattr(session_module, "Engine", MagicMock())
    monkeypatch.setattr(session_module, "AsyncEngine", MagicMock(dispose=AsyncMock()))
    session_module._pooled_async_engines.clear()
    session_module._pooled_async_engines.update({1: failing, 2: healthy})

    asyncio.run(session_module.close_database())

    healthy.dispose.assert_awaited_once()


def test_close_database_tolerates_global_engine_failure(monkeypatch):
    """
    全局引擎释放失败不能抛出——close_database 在关闭流程末尾调用，
    抛异常会掩盖其他关闭步骤的问题。
    """
    monkeypatch.setattr(session_module, "Engine",
                        MagicMock(dispose=MagicMock(side_effect=RuntimeError("boom"))))
    monkeypatch.setattr(session_module, "AsyncEngine", MagicMock(dispose=AsyncMock()))
    session_module._pooled_async_engines.clear()

    asyncio.run(session_module.close_database())  # 不抛异常即通过


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
