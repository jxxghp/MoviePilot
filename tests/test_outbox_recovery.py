"""
持久恢复的阻塞隔离、租约所有权、关闭屏障与重试时间回归。
"""

import asyncio
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Event
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.application import outbox
from app.application.outbox import OutboxDispatcher, OutboxIntent, OutboxRecovery
from app.db.adapters.outbox import SqlAlchemyOutboxDispatchStore, SqlAlchemyOutboxStager
from app.db.models.outbox import OutboxMessage


@pytest.fixture
def database(tmp_path):
    """
    创建支持独立线程短事务的临时文件数据库与可推进时钟。
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'outbox.db'}")
    OutboxMessage.__table__.create(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    now = [datetime(2026, 10, 17, tzinfo=timezone.utc)]
    yield factory, SqlAlchemyOutboxDispatchStore(factory), now
    engine.dispose()


def stage(factory, now, *topics):
    """
    按给定顺序暂存具备独立稳定键的消息。
    """
    with factory() as session:
        for index, topic in enumerate(topics):
            SqlAlchemyOutboxStager(session).stage(OutboxIntent(f"{topic}:{index}", topic, {}), now)
        session.commit()


def rows(factory):
    """
    读取脱离 Session 的快照，避免测试跨线程共享会话。
    """
    with factory() as session:
        return session.scalars(select(OutboxMessage).order_by(OutboxMessage.id)).all()


def test_blocked_transfer_does_not_block_notification_or_reclaim_live_owner(database):
    """
    整理积压只占一个位置，下载通知可完成；超出租期仍不重复认领活跃效果。
    """
    factory, store, now = database
    stage(factory, now[0], *(["transfer.completed"] * 16), "download.added.notification")
    started, release, notified, closed = Event(), Event(), Event(), Event()
    received = []

    def transfer(message):
        """
        首条整理事件停在屏障，其余消息记录实际执行顺序。
        """
        received.append(message.message_id)
        if len(received) == 1:
            started.set()
            assert release.wait(5)

    def notify(_message):
        """
        记录通知已运行，不依赖真实 Telegram。
        """
        notified.set()

    def provider():
        """
        每个恢复 worker 获取独立 dispatcher 生命周期。
        """
        return OutboxDispatcher(store, {
            "transfer.completed": transfer, "download.added.notification": notify,
        }, clock=lambda: now[0], close=closed.set)

    with ThreadPoolExecutor(max_workers=2) as pool:
        recovery = OutboxRecovery(provider, pool.submit, max_inflight=2)
        try:
            assert recovery.dispatch(20) == 2
            assert started.wait(2) and notified.wait(2) and closed.wait(2)
            for seconds in (61, 122):
                now[0] += timedelta(seconds=seconds)
                recovery.dispatch(20)
                snapshot = rows(factory)
                assert snapshot[0].attempt == 1
                assert snapshot[0].lease_until == (now[0] + timedelta(seconds=60)).isoformat()
                assert all(row.status == "pending" for row in snapshot[1:16])
                assert snapshot[-1].status == "completed"
                assert received == [1]
            release.set()
        finally:
            release.set()
            assert recovery.stop(5)
    # stop 封口后不再领取下一条，重启后的新恢复器可继续消化剩余批次。
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = []

        def submit(operation):
            """
            保留真实 worker 句柄，等待批次执行完再关闭。
            """
            future = pool.submit(operation)
            futures.append(future)
            return future

        recovery = OutboxRecovery(provider, submit)
        try:
            recovery.dispatch(20)
            for future in futures:
                future.result(timeout=5)
        finally:
            assert recovery.stop(5)
    assert received == list(range(1, 17))
    assert all(row.status == "completed" for row in rows(factory))


def test_stop_retains_owner_and_rejects_reset_until_worker_finishes(database, monkeypatch):
    """
    关闭超时既不撤销 owner 也不释放资源，禁止重新装配覆盖活跃批次。
    """
    factory, store, now = database
    stage(factory, now[0], "blocked", "later")
    started, release = Event(), Event()
    close = MagicMock()

    def block(_message):
        """
        模拟等待外部系统的已开始效果。
        """
        started.set()
        assert release.wait(5)

    def provider():
        """
        绑定测试时钟及资源关闭观测。
        """
        return OutboxDispatcher(store, {"blocked": block}, clock=lambda: now[0], close=close)

    monkeypatch.setattr(outbox, "_configured_recovery", None)
    monkeypatch.setattr(outbox, "_configured_dispatcher", None)
    with ThreadPoolExecutor(max_workers=1) as pool:
        outbox.configure_outbox_dispatcher(provider, submit=pool.submit)
        try:
            assert outbox.dispatch_pending_outbox(1) == 1 and started.wait(2)
            assert not outbox.stop_outbox_recovery(0)
            close.assert_not_called()
            assert outbox.dispatch_pending_outbox(20) == 0
            with pytest.raises(RuntimeError, match="尚未停止"):
                outbox.reset_outbox_dispatcher()
            with pytest.raises(RuntimeError, match="尚未停止"):
                outbox.configure_outbox_dispatcher(provider, submit=pool.submit)
        finally:
            release.set()
            assert outbox.stop_outbox_recovery(5)
            outbox.reset_outbox_dispatcher()
    close.assert_called_once()
    assert [row.status for row in rows(factory)] == ["completed", "pending"]


@pytest.mark.parametrize("cancel", [False, True])
def test_unstarted_worker_releases_lease_and_capacity(database, cancel):
    """
    提交拒绝或排队取消都保留重试记录、释放资源与准入容量。
    """
    factory, store, now = database
    stage(factory, now[0], "notification")
    close, handler = MagicMock(), MagicMock()
    future = Future()

    def submit(_operation):
        """
        模拟已封口线程池与尚未开始的可取消任务。
        """
        if not cancel:
            raise RuntimeError("executor closed")
        return future

    recovery = OutboxRecovery(lambda: OutboxDispatcher(
        store, {"notification": handler}, clock=lambda: now[0], close=close,
    ), submit, max_inflight=1)
    if cancel:
        assert recovery.dispatch(1) == 1
        assert recovery.dispatch(1) == 0
        assert future.cancel()
    else:
        with pytest.raises(RuntimeError, match="executor closed"):
            recovery.dispatch(1)
    assert recovery.stop(0)
    close.assert_called_once()
    handler.assert_not_called()
    row = rows(factory)[0]
    assert row.status == "pending" and row.lease_until is None
    assert row.next_retry_at == (now[0] + timedelta(seconds=1)).isoformat()


def test_expired_owner_recovers_with_fenced_renewal(database):
    """
    崩溃遗留 processing 能重新认领，旧 attempt 无法续租或覆盖新结果。
    """
    factory, store, now = database
    stage(factory, now[0], "notification")
    old = store.claim(now[0], now[0] + timedelta(seconds=60))
    now[0] += timedelta(seconds=61)
    new = store.claim(now[0], now[0] + timedelta(seconds=60))
    assert old.event_key == new.event_key and new.attempt == old.attempt + 1
    assert not store.renew(old.message_id, old.attempt, now[0] + timedelta(hours=1))
    assert not store.complete(old.message_id, old.attempt, now[0])
    assert store.complete(new.message_id, new.attempt, now[0])
    assert not store.renew(new.message_id, new.attempt, now[0] + timedelta(hours=1))


@pytest.mark.parametrize("fail", [False, True])
def test_settlement_uses_finish_time(database, fail):
    """
    慢处理的完成时间与退避起点均取结束时刻，避免失败立即再次重试。
    """
    factory, store, now = database
    stage(factory, now[0], "notification")

    def handler(_message):
        """
        模拟发送消耗九十秒，覆盖超过 lease 的同步结算时间。
        """
        now[0] += timedelta(seconds=90)
        if fail:
            raise RuntimeError("offline")

    dispatcher = OutboxDispatcher(store, {"notification": handler}, clock=lambda: now[0])
    assert dispatcher.dispatch_one()
    row = rows(factory)[0]
    if fail:
        assert row.status == "pending" and row.last_error == "offline"
        assert row.next_retry_at == (now[0] + timedelta(seconds=1)).isoformat()
        assert not dispatcher.dispatch_one()
    else:
        assert row.completed_at == now[0].isoformat()


@pytest.mark.parametrize("asynchronous", [False, True])
def test_scheduler_stop_preserves_dependencies_on_recovery_timeout(monkeypatch, asynchronous):
    """
    同步和异步关闭都先等恢复 worker，超时不能清除下游绑定。
    """
    from app.startup.initializers import scheduler as initializer

    scheduler, reset = MagicMock(), MagicMock()

    async def stop_async():
        """
        模拟调度器异步关闭完成。
        """
        return None

    scheduler.stop_async = stop_async
    monkeypatch.setattr(initializer, "Scheduler", lambda: scheduler)
    monkeypatch.setattr(initializer, "reset_scheduler_bindings", reset)
    monkeypatch.setattr(initializer, "stop_outbox_recovery", lambda: False)

    async def shutdown():
        """
        在真实事件循环中调用关闭入口。
        """
        await initializer.stop_scheduler()

    with pytest.raises(RuntimeError, match="Outbox 后台处理尚未停止"):
        if asynchronous:
            asyncio.run(shutdown())
        else:
            initializer.stop_scheduler()
    reset.assert_not_called()


def test_scheduler_shutdown_keeps_event_loop_available(monkeypatch):
    """
    关闭屏障等待线程期间，事件循环仍能完成该线程等待的异步 handler。
    """
    from app.startup.initializers import scheduler as initializer

    started, release = Event(), Event()
    reset = MagicMock()

    def wait_for_worker():
        """
        模拟 worker 正等待事件循环上的处理返回。
        """
        started.set()
        return release.wait(3)

    async def no_op():
        """
        模拟已停止的定时器。
        """

    scheduler = MagicMock()
    scheduler.stop_async = no_op
    monkeypatch.setattr(initializer, "Scheduler", lambda: scheduler)
    monkeypatch.setattr(initializer, "reset_scheduler_bindings", reset)
    monkeypatch.setattr(initializer, "stop_outbox_recovery", wait_for_worker)

    async def shutdown():
        """
        并发运行生命周期等待和异步 handler，确认等待不会占住事件循环。
        """
        async def finish_handler():
            """
            屏障启动后由事件循环放行 worker。
            """
            while not started.is_set():
                await asyncio.sleep(0)
            reset.assert_not_called()
            release.set()

        await asyncio.gather(initializer.stop_scheduler(), finish_handler())

    try:
        asyncio.run(shutdown())
    finally:
        release.set()
    reset.assert_called_once()


def test_claim_error_closes_dispatcher(database):
    """
    认领失败不能泄漏工厂资源或登记不存在的 topic owner。
    """
    _, store, now = database
    store = MagicMock(wraps=store)
    store.claim.side_effect = RuntimeError("database unavailable")
    close, submit = MagicMock(), MagicMock()
    recovery = OutboxRecovery(lambda: OutboxDispatcher(
        store, {}, clock=lambda: now[0], close=close,
    ), submit)
    with pytest.raises(RuntimeError, match="database unavailable"):
        recovery.dispatch(20)
    assert recovery.stop(0)
    close.assert_called_once()
    submit.assert_not_called()


def test_worker_settlement_failure_is_observed_and_releases_owner(database, monkeypatch):
    """
    结算数据库故障保留 processing 租约用于恢复，同时回收已退出 worker 的容量。
    """
    factory, store, now = database
    stage(factory, now[0], "notification")
    monkeypatch.setattr(store, "complete", MagicMock(side_effect=RuntimeError("database unavailable")))
    logged, closed = MagicMock(), Event()
    monkeypatch.setattr(outbox.logger, "error", logged)
    with ThreadPoolExecutor(max_workers=1) as pool:
        recovery = OutboxRecovery(lambda: OutboxDispatcher(
            store, {"notification": lambda _message: None}, clock=lambda: now[0], close=closed.set,
        ), pool.submit, max_inflight=1)
        try:
            assert recovery.dispatch(1) == 1 and closed.wait(2)
        finally:
            assert recovery.stop(5)
    logged.assert_called_once()
    assert rows(factory)[0].status == "processing"
