"""
异步数据库连接池的按事件循环池化测试。

NullPool 下每个异步会话独占一条物理连接、零复用且无上限：调度器以上百个线程向
主事件循环投递协程，突发并发会直接顶穿 PostgreSQL 的 max_connections；SQLite 侧
则表现为 WAL 写争用与反复 checkpoint 导致的长时间卡顿。

NullPool 被选用的唯一理由是「永不复用」从而「永不跨事件循环」——asyncpg 的
Connection 与 aiosqlite 的线程都绑定在创建它的循环上。因此池化必须严格按循环
隔离：常驻主循环用池，其余循环回退 NullPool。

这些测试固定三项不变量：只有常驻主循环被池化、池化引擎按循环隔离且可复用、
回退路径受全局配额约束。
"""
import asyncio
import threading
from unittest.mock import patch

import pytest

# 池化实现位于 app.db.session；app.db 只做 re-export，私有符号不在其上
import app.db.session as db_module
from app.runtime.config import global_vars, settings
from app.db.engine import _async_pool_kwargs, get_global_async_engine

# 用 getter 而不是旧名字 AsyncEngine：后者只为仓库外插件保留，模块级导入它会在 pytest
# 的**收集期**就把全局异步引擎建出来——用例还一个没跑，引擎已经在了。getter 是同一个
# 单例，下面那几处 `is` 断言的语义分毫不差。


@pytest.fixture(autouse=True)
def _restore_state():
    """
    每个用例后复原全局状态，避免污染其他测试。
    """
    saved_loop = global_vars.CURRENT_EVENT_LOOP
    saved_engines = dict(db_module._pooled_async_engines)
    yield
    global_vars.CURRENT_EVENT_LOOP = saved_loop
    db_module._pooled_async_engines.clear()
    db_module._pooled_async_engines.update(saved_engines)


def test_pool_disabled_falls_back_to_nullpool(monkeypatch):
    """
    配置为 NullPool 时必须完全回到池化前的行为，作为兼容性逃生舱。
    """
    monkeypatch.setattr(settings, "DB_ASYNC_POOL_TYPE", "NullPool", raising=False)

    async def run():
        global_vars.CURRENT_EVENT_LOOP = asyncio.get_running_loop()
        return db_module.get_async_engine()

    assert asyncio.run(run()) is get_global_async_engine()


def test_non_resident_loop_is_not_pooled():
    """
    非常驻循环必须回退 NullPool。

    池中连接绑定在创建它的循环上，临时循环销毁后连接即失效；若对其池化，
    下次复用会抛 "attached to a different loop"。
    """
    async def run():
        # 当前运行的循环不是注册的常驻循环
        global_vars.CURRENT_EVENT_LOOP = None
        return db_module.get_async_engine()

    assert asyncio.run(run()) is get_global_async_engine()


def test_no_running_loop_falls_back():
    """
    没有运行中的事件循环时不得池化，行为与池化前一致。
    """
    assert db_module.get_async_engine() is get_global_async_engine()


def test_pooled_loop_gets_dedicated_engine_and_reuses_it(monkeypatch):
    """
    常驻主循环应获得独立的池化引擎，且同一循环内必须复用同一个引擎实例
    ——每次新建引擎等于每次新建一个池，池化就失去了意义。
    """
    monkeypatch.setattr(settings, "DB_ASYNC_POOL_TYPE", "QueuePool", raising=False)

    async def run():
        global_vars.CURRENT_EVENT_LOOP = asyncio.get_running_loop()
        db_module._pooled_async_engines.clear()
        first = db_module.get_async_engine()
        second = db_module.get_async_engine()
        return first, second

    first, second = asyncio.run(run())
    assert first is not get_global_async_engine(), "常驻循环没有拿到池化引擎"
    assert first is second, "同一循环重复创建了引擎，池被反复丢弃"


def test_engine_is_isolated_per_loop(monkeypatch):
    """
    不同事件循环必须拿到不同的引擎实例，绝不能共用一个池。
    """
    monkeypatch.setattr(settings, "DB_ASYNC_POOL_TYPE", "QueuePool", raising=False)
    db_module._pooled_async_engines.clear()
    engines = []

    def run_in_own_loop():
        """
        在独立线程的独立事件循环中取一次引擎。
        """
        async def inner():
            global_vars.CURRENT_EVENT_LOOP = asyncio.get_running_loop()
            engines.append(db_module.get_async_engine())

        asyncio.run(inner())

    for _ in range(2):
        thread = threading.Thread(target=run_in_own_loop)
        thread.start()
        thread.join()

    assert len(engines) == 2
    assert engines[0] is not engines[1], "两个事件循环共用了同一个连接池"


def test_pool_kwargs_shape():
    """
    池化时不得指定 poolclass：SQLAlchemy 需要自行选用异步适配的
    AsyncAdaptedQueuePool，显式传入同步 QueuePool 会出错。
    """
    pooled = _async_pool_kwargs(True)
    assert "poolclass" not in pooled
    assert pooled["pool_size"] == settings.DB_ASYNC_POOL_SIZE
    assert pooled["max_overflow"] == settings.DB_ASYNC_MAX_OVERFLOW

    fallback = _async_pool_kwargs(False)
    assert fallback["poolclass"].__name__ == "NullPool"


def test_fallback_slot_is_released(monkeypatch):
    """
    回退路径的配额必须在会话结束后归还，否则连续调用会把自己饿死。
    """
    monkeypatch.setattr(settings, "DB_ASYNC_POOL_TYPE", "NullPool", raising=False)

    async def run():
        before = db_module._fallback_slots._value
        for _ in range(3):
            async with db_module.async_session_scope():
                pass
        return before, db_module._fallback_slots._value

    before, after = asyncio.run(run())
    assert before == after, "配额未归还，回退路径会逐步耗尽"


def test_fallback_slot_times_out_when_exhausted(monkeypatch):
    """
    配额耗尽时必须抛出明确错误，而不是无限等待或静默失败
    ——这正是 NullPool 缺失的背压。
    """
    monkeypatch.setattr(settings, "DB_POOL_TIMEOUT", 0.05, raising=False)
    monkeypatch.setattr(settings, "DB_ASYNC_FALLBACK_LIMIT", 1, raising=False)
    monkeypatch.setattr(db_module, "_fallback_slots", threading.BoundedSemaphore(1))

    async def run():
        db_module._fallback_slots.acquire()  # 占满唯一名额
        with patch("app.db.session.record_metric") as record_metric:
            with pytest.raises(TimeoutError):
                await db_module._acquire_fallback_slot()
        record_metric.assert_any_call("db.pool.timeout", backend="sqlite")
        record_metric.assert_any_call(
            "db.pool.wait",
            pytest.approx(0.05, abs=0.03),
            backend="sqlite",
            outcome="timeout",
        )

    asyncio.run(run())


def test_pooled_path_does_not_consume_fallback_quota(monkeypatch):
    """
    池化路径由连接池自身限流，不应再占用回退配额
    ——否则主循环流量会把兜底名额吃光，临时循环反而被饿死。
    """
    monkeypatch.setattr(settings, "DB_ASYNC_POOL_TYPE", "QueuePool", raising=False)

    async def run():
        global_vars.CURRENT_EVENT_LOOP = asyncio.get_running_loop()
        db_module._pooled_async_engines.clear()
        before = db_module._fallback_slots._value
        async with db_module.async_session_scope():
            during = db_module._fallback_slots._value
        return before, during

    before, during = asyncio.run(run())
    assert before == during, "池化路径不应消耗回退配额"
