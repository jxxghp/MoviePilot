"""
持久事件逐处理器投递的批次隔离、事务快照、重试和生命周期回归。
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Event
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.application.outbox import (
    DURABLE_EVENT_TOPICS,
    EVENT_FANOUT_COMPLETE_FIELD,
    EVENT_HANDLER_TOPIC_PREFIX,
    OutboxDispatcher,
    OutboxIntent,
    OutboxLeaseLostError,
    OutboxRecovery,
    durable_event_topic,
)
from app.db.adapters.outbox import SqlAlchemyOutboxDispatchStore, SqlAlchemyOutboxStager
from app.db.models.outbox import OutboxMessage
from app.runtime.correlation import get_correlation_id
from app.runtime.event.errors import EventErrorPolicy
from app.runtime.event.registry import EventRegistry
from app.runtime.events import eventmanager
from app.runtime.loop import main_loop_registry
from app.schemas.transfer import TransferInfo
from app.schemas.types import EventType
from app.startup.composition import outbox as composition


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    """
    隔离事件订阅、错误通知和文件数据库，真实执行宿主投递及租约边界。
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'events.db'}")
    OutboxMessage.__table__.create(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    for attribute, value in {
        "__broadcast_subscribers": {},
        "__disabled_handlers": set(),
        "__disabled_classes": set(),
        "__handler_instance_resolvers": {},
        "__lifecycle_state": "running",
        "__error_policy": EventErrorPolicy(notifier=lambda: None, emit_system_error=lambda _data: None),
    }.items():
        monkeypatch.setattr(eventmanager, f"_EventManager{attribute}", value)
    monkeypatch.setattr(composition, "SessionFactory", factory)
    monkeypatch.setattr(composition, "EventManager", lambda: eventmanager)
    yield factory, SqlAlchemyOutboxDispatchStore(factory)
    engine.dispose()


def _stage(factory, count=1, event_type=EventType.TransferComplete):
    """
    暂存保持旧插件载荷格式的整理或订阅事件，返回稳定业务事件键。
    """
    keys = []
    with factory() as session:
        for index in range(count):
            key = f"{durable_event_topic(event_type)}:task-{index}:v1"
            keys.append(key)
            SqlAlchemyOutboxStager(session).stage(
                OutboxIntent(key, durable_event_topic(event_type), {
                    "sequence": index,
                    "subscribe_id": index,
                    "transferinfo": {"success": True},
                }),
                datetime.now(timezone.utc),
            )
        session.commit()
    return keys


def _rows(factory):
    """
    返回脱离会话的消息快照，便于核对跨线程结算。
    """
    with factory() as session:
        return session.scalars(select(OutboxMessage).order_by(OutboxMessage.id)).all()


def _expand(factory, count=1):
    """
    只处理先写入的父事件，确认此阶段不执行任何插件代码。
    """
    dispatcher = composition.build_outbox_dispatcher()
    for _ in range(count):
        assert dispatcher.dispatch_one()
    return [row for row in _rows(factory) if row.topic.startswith(EVENT_HANDLER_TOPIC_PREFIX)]


def test_blocked_handler_does_not_delay_the_other_handlers_entire_batch(runtime):
    """
    首个插件未返回时另一个插件已收到全部十二集，慢插件仍持有租约和关闭屏障。
    """
    factory, store = runtime
    started, release, received_all = Event(), Event(), Event()
    fast_calls, slow_calls = [], []

    def slow(event):
        """
        用可释放屏障代替插件的六十秒等待，不遗留后台执行。
        """
        slow_calls.append(event.event_data["sequence"])
        started.set()
        assert release.wait(10)

    def fast(event):
        """
        验证对象恢复与关联标识，并记录整批接收顺序。
        """
        assert isinstance(event.event_data["transferinfo"], TransferInfo)
        assert event.correlation_id == get_correlation_id() == event.event_data["idempotency_key"]
        fast_calls.append(event.event_data["sequence"])
        if len(fast_calls) == 12:
            received_all.set()

    eventmanager.add_event_listener(EventType.TransferComplete, slow)
    eventmanager.add_event_listener(EventType.TransferComplete, fast)
    keys = _stage(factory, 12)
    children = _expand(factory, 12)
    assert len(children) == 24
    assert slow_calls == fast_calls == []
    assert all(row.status == "completed" for row in _rows(factory)[:12])
    assert {row.payload["event_key"] for row in children} == set(keys)

    with ThreadPoolExecutor(max_workers=2) as pool:
        recovery = OutboxRecovery(composition.build_outbox_dispatcher, pool.submit, max_inflight=2)
        try:
            assert recovery.dispatch(20) == 2
            assert started.wait(2)
            assert received_all.wait(5)
            assert fast_calls == list(range(12))
            assert slow_calls == [0]
            # 模拟租约过期后再次扫描，活跃处理器仍不允许启动第二个拥有者。
            slow_row = next(row for row in _rows(factory) if row.status == "processing")
            with factory() as session:
                session.get(OutboxMessage, slow_row.id).lease_until = "2000-01-01T00:00:00+00:00"
                session.commit()
            recovery.dispatch(20)
            renewed = next(row for row in _rows(factory) if row.id == slow_row.id)
            assert renewed.attempt == 1
            assert renewed.lease_until > datetime.now(timezone.utc).isoformat()
            assert not recovery.stop(0)
            assert slow_calls == [0]
        finally:
            release.set()
            assert recovery.stop(5)
    assert len(slow_calls) == 1
    assert sum(row.status == "pending" for row in _rows(factory)) == 11
    # 新恢复器重放尚未认领的记录，已经成功的接收方不会再收到整批事件。
    dispatcher = composition.build_outbox_dispatcher()
    while dispatcher.dispatch_one():
        pass
    assert slow_calls == list(range(12))
    assert fast_calls == list(range(12))
    assert all(row.status == "completed" for row in _rows(factory))
    assert store.claim(datetime.now(timezone.utc), datetime.now(timezone.utc)) is None


def test_parent_replay_reuses_committed_snapshot_even_after_subscriptions_change(runtime):
    """
    展开成功但父消息结算前崩溃时，重放不会再次投递或把新订阅加入旧事件。
    """
    factory, store = runtime
    original, added_later = Mock(), Mock()

    def first(event):
        """
        记录原订阅处理器收到的稳定事件键。
        """
        original(event.event_data["idempotency_key"])

    def later(event):
        """
        后加入的订阅不应进入已冻结的父事件快照。
        """
        added_later(event)

    eventmanager.add_event_listener(EventType.TransferComplete, first)
    keys = _stage(factory)
    now = datetime.now(timezone.utc)
    parent = store.claim(now, now + timedelta(seconds=1))
    composition.build_outbox_handlers()[parent.topic](parent)
    assert _rows(factory)[0].payload[EVENT_FANOUT_COMPLETE_FIELD]
    eventmanager.add_event_listener(EventType.TransferComplete, later)
    replay = store.claim(now + timedelta(seconds=2), now + timedelta(seconds=62))
    assert replay.message_id == parent.message_id and replay.attempt == 2
    composition.build_outbox_handlers()[replay.topic](replay)
    assert store.complete(replay.message_id, replay.attempt, now)
    # 模拟进程恢复，使用新的派发器与短会话完成已持久化的子消息。
    assert composition.build_outbox_dispatcher().dispatch_one()
    assert not composition.build_outbox_dispatcher().dispatch_one()
    original.assert_called_once_with(keys[0])
    added_later.assert_not_called()
    assert len(_rows(factory)) == 2


def test_fanout_transaction_rolls_back_all_children_and_marker(runtime):
    """
    子记录写入冲突时全事务回滚，父消息仍可安全重试展开。
    """
    factory, store = runtime
    _stage(factory)
    now = datetime.now(timezone.utc)
    parent = store.claim(now, now + timedelta(seconds=60))
    duplicate = OutboxIntent("same-child", "event.handler.test", {})
    with pytest.raises(IntegrityError):
        store.stage_fanout(parent, (duplicate, duplicate), now)
    rows = _rows(factory)
    assert len(rows) == 1
    assert not rows[0].payload.get(EVENT_FANOUT_COMPLETE_FIELD)
    store.stage_fanout(parent, (duplicate,), now)
    assert len(_rows(factory)) == 2


def test_stale_parent_cannot_expand_event(runtime):
    """
    旧 attempt 不得写入子消息或覆盖新拥有者的展开状态。
    """
    factory, store = runtime
    _stage(factory)
    now = datetime.now(timezone.utc)
    stale = store.claim(now, now + timedelta(seconds=1))
    current = store.claim(now + timedelta(seconds=2), now + timedelta(seconds=62))
    assert current.attempt == 2
    with pytest.raises(OutboxLeaseLostError):
        store.stage_fanout(stale, (OutboxIntent("stale-child", "event.handler.test", {}),), now)
    assert len(_rows(factory)) == 1


def test_handler_failure_only_retries_its_own_delivery(runtime):
    """
    失败处理器重试至死信，已完成的同事件处理器只执行一次。
    """
    factory, store = runtime
    attempts, successful = [], []

    def failing(event):
        """
        模拟每次投递均失败的第三方处理器。
        """
        attempts.append(event.event_data["idempotency_key"])
        raise RuntimeError("插件事件失败")

    def peer(event):
        """
        记录不应跟随其他处理器重试的成功接收方。
        """
        successful.append(event.event_data["idempotency_key"])

    eventmanager.add_event_listener(EventType.TransferComplete, failing)
    eventmanager.add_event_listener(EventType.TransferComplete, peer)
    keys = _stage(factory)
    _expand(factory)
    now = [datetime.now(timezone.utc)]
    dispatcher = OutboxDispatcher(store, composition.build_outbox_handlers(),
                                 fallback_handler=composition.dispatch_event_handler, clock=lambda: now[0])
    assert dispatcher.dispatch_one() and dispatcher.dispatch_one()
    for _ in range(4):
        now[0] += timedelta(hours=1)
        assert dispatcher.dispatch_one()
    assert not dispatcher.dispatch_one()
    assert attempts == keys * 5
    assert successful == keys
    rows = _rows(factory)
    assert [row.status for row in rows] == ["completed", "dead", "completed"]
    assert rows[1].payload["handler_id"] == EventRegistry.handler_identifier(failing)


@pytest.mark.parametrize("event_type", list(DURABLE_EVENT_TOPICS))
def test_all_durable_topics_keep_payload_and_disabled_handler_semantics(runtime, event_type):
    """
    所有持久事件保留业务载荷与幂等键，仅为启用处理器创建可恢复投递。
    """
    factory, _store = runtime
    received = []

    def enabled(event):
        """
        记录实际收到的事件类型、数据和关联上下文。
        """
        received.append((event.event_type, event.event_data, event.correlation_id))

    def disabled(_event):
        """
        禁用处理器不能执行，也不能生成子投递记录。
        """
        raise AssertionError("禁用处理器不应执行")

    eventmanager.add_event_listener(event_type, enabled)
    eventmanager.add_event_listener(event_type, disabled)
    eventmanager.disable_event_handler(disabled)
    keys = _stage(factory, event_type=event_type)
    children = _expand(factory)
    assert len(children) == 1
    assert len(children[0].topic) <= 100 and len(children[0].event_key) <= 255
    assert children[0].payload["handler_id"] == EventRegistry.handler_identifier(enabled)
    assert composition.build_outbox_dispatcher().dispatch_one()
    assert len(received) == 1
    kind, payload, correlation_id = received[0]
    assert kind == event_type
    assert payload["idempotency_key"] == keys[0] == correlation_id
    assert payload["sequence"] == 0


def test_unregistered_target_is_retryable_and_does_not_call_peers(runtime):
    """
    插件重载窗口内目标缺失时保留待重试消息，恢复同一标识后可继续投递。
    """
    factory, store = runtime
    received = []

    def handler(event):
        """
        收集重载后接续投递的事件键。
        """
        received.append(event.event_data["idempotency_key"])

    eventmanager.add_event_listener(EventType.TransferComplete, handler)
    keys = _stage(factory)
    _expand(factory)
    eventmanager.remove_event_listener(EventType.TransferComplete, handler)
    dispatcher = composition.build_outbox_dispatcher()
    assert dispatcher.dispatch_one()
    assert _rows(factory)[1].status == "pending"
    assert "尚未注册" in _rows(factory)[1].last_error
    eventmanager.add_event_listener(EventType.TransferComplete, handler)
    later = datetime.now(timezone.utc) + timedelta(seconds=2)
    replay = store.claim(later, later + timedelta(seconds=60))
    dispatcher.dispatch_claimed(replay)
    assert received == keys


@pytest.mark.anyio
async def test_async_handler_waits_for_real_completion_without_blocking_peer(runtime):
    """
    协程插件继续在主循环执行，独立线程拥有租约直到异步处理实际返回。
    """
    factory, _store = runtime
    started, release, peer_done = asyncio.Event(), asyncio.Event(), Event()

    async def slow(_event):
        """
        用协程屏障保持处理器在途，验证没有提前结算。
        """
        started.set()
        await release.wait()

    def peer(_event):
        """
        同事件的同步接收方应在协程屏障释放前完成。
        """
        peer_done.set()

    eventmanager.add_event_listener(EventType.TransferComplete, slow)
    eventmanager.add_event_listener(EventType.TransferComplete, peer)
    _stage(factory)
    _expand(factory)
    loop_owner = main_loop_registry.register(asyncio.get_running_loop())
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            recovery = OutboxRecovery(composition.build_outbox_dispatcher, pool.submit)
            try:
                assert recovery.dispatch(20) == 2
                await asyncio.wait_for(started.wait(), 2)
                assert await asyncio.to_thread(peer_done.wait, 2)
                assert any(row.status == "processing" for row in _rows(factory))
                assert not recovery.stop(0)
            finally:
                release.set()
                assert await asyncio.to_thread(recovery.stop, 5)
    finally:
        main_loop_registry.release(loop_owner)
    assert all(row.status == "completed" for row in _rows(factory))


@pytest.mark.parametrize("topic,event_type,with_fallback", [
    ("unknown", EventType.TransferComplete, True),
    ("unknown", EventType.TransferComplete, False),
    (f"{EVENT_HANDLER_TOPIC_PREFIX}invalid", EventType.ConfigChanged, True),
])
def test_unknown_topic_or_event_remains_a_failed_delivery(runtime, topic, event_type, with_fallback):
    """
    动态主题回调不接受未登记主题，不能把未知效果误记为成功。
    """
    factory, store = runtime
    with factory() as session:
        SqlAlchemyOutboxStager(session).stage(
            OutboxIntent("unknown", topic, {"event_type": event_type.value}), datetime.now(timezone.utc),
        )
        session.commit()
    dispatcher = composition.build_outbox_dispatcher() if with_fallback else OutboxDispatcher(store, {})
    assert dispatcher.dispatch_one()
    row = _rows(factory)[0]
    assert row.status == "pending"
    assert ("未登记" if with_fallback else "unknown") in row.last_error


def test_stopped_bus_cannot_settle_parent_or_child_delivery(runtime, monkeypatch):
    """
    总线封口后不能把尚未展开的父消息或未执行的子投递误记为完成。
    """
    factory, store = runtime

    def handler(_event):
        """
        封口后的任何调用都应先被生命周期边界拒绝。
        """
        raise AssertionError("停止状态不应执行处理器")

    eventmanager.add_event_listener(EventType.TransferComplete, handler)
    _stage(factory, 2)
    _expand(factory)
    monkeypatch.setattr(eventmanager, "_EventManager__lifecycle_state", "stopping")
    dispatcher = composition.build_outbox_dispatcher()
    assert dispatcher.dispatch_one() and dispatcher.dispatch_one()
    assert not dispatcher.dispatch_one()
    rows = _rows(factory)
    assert [row.status for row in rows] == ["completed", "pending", "pending"]
    assert all("stopping" in row.last_error for row in rows[1:])
    assert not rows[1].payload.get(EVENT_FANOUT_COMPLETE_FIELD)
    assert store.claim(datetime.now(timezone.utc), datetime.now(timezone.utc)) is None
