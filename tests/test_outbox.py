"""durable side-effect outbox 原子性、认领、重试与稳定重放测试。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.application.maintenance import CleanupPolicy, DataCleanupService
from app.application.outbox import (
    ClaimedOutboxMessage,
    OutboxDispatcher,
    OutboxIntent,
    OutboxLeaseLostError,
)
from app.application.subscription.contract import (
    SubscriptionIdentity,
    SubscriptionPatch,
)
from app.application.subscription.write import CreateSubscriptionCommand
from app.db.adapters.outbox import (
    SqlAlchemyOutboxDispatchStore,
    SqlAlchemyOutboxStager,
)
from app.db.base import Base
from app.db.maintenance import DatabaseCleanupRepository
from app.db.models.outbox import OutboxMessage
from app.schemas.types import MediaSource


class _Staged:
    """测试用新订阅暂存结果。"""

    subscribe_id = 42
    message = "ok"
    created = True


def test_subscription_and_outbox_intent_commit_together() -> None:
    """业务行与 intent 均 stage 成功后才允许同一次 commit。"""
    calls = []
    repository = MagicMock()
    repository.stage_add.side_effect = lambda *_args: calls.append("subscription") or _Staged()
    outbox = MagicMock()
    outbox.stage.side_effect = lambda *_args: calls.append("outbox")
    unit_of_work = MagicMock()
    unit_of_work.commit.side_effect = lambda: calls.append("commit")
    command = CreateSubscriptionCommand(repository, unit_of_work, outbox=outbox)

    result = command.execute(
        SubscriptionIdentity(media_source=MediaSource.TMDB, media_id="unknown"),
        SubscriptionPatch({"name": "demo"}),
        "user",
    )

    assert result == (42, "ok")
    assert calls == ["subscription", "outbox", "outbox", "commit"]
    intent = outbox.stage.call_args_list[0].args[0]
    assert intent.event_key == "subscribe.added:42:unknown:unknown:v1"
    assert intent.payload["subscribe_id"] == 42
    report_intent = outbox.stage.call_args_list[1].args[0]
    assert report_intent.topic == "subscribe.added.report"
    assert report_intent.event_key.endswith(":report")


def test_subscription_notification_snapshot_is_part_of_same_transaction() -> None:
    """订阅新增通知快照与事件、统计意图一起暂存，便于崩溃恢复。"""
    calls = []
    repository = MagicMock()
    repository.stage_add.side_effect = lambda *_args: calls.append("subscription") or _Staged()
    outbox = MagicMock()
    outbox.stage.side_effect = lambda *_args: calls.append("outbox")
    unit_of_work = MagicMock()
    unit_of_work.commit.side_effect = lambda: calls.append("commit")
    command = CreateSubscriptionCommand(repository, unit_of_work, outbox=outbox)

    command.execute(
        SubscriptionIdentity(media_source=MediaSource.TMDB, media_id="unknown"),
        SubscriptionPatch({"name": "demo"}),
        "user",
        notification={"title": "订阅成功", "text": "demo"},
    )

    intents = [call.args[0] for call in outbox.stage.call_args_list]
    assert [intent.topic for intent in intents] == [
        "subscribe.added",
        "subscribe.added.notification",
        "subscribe.added.report",
    ]
    assert intents[1].payload["message"]["text"] == "demo"
    assert calls[-1] == "commit"


def test_outbox_stage_failure_rolls_back_business_transaction() -> None:
    """intent 无法持久化时订阅行不得单独提交。"""
    repository = MagicMock()
    repository.stage_add.return_value = _Staged()
    outbox = MagicMock()
    outbox.stage.side_effect = RuntimeError("outbox unavailable")
    unit_of_work = MagicMock()
    command = CreateSubscriptionCommand(repository, unit_of_work, outbox=outbox)

    with pytest.raises(RuntimeError, match="outbox unavailable"):
        command.execute(
            SubscriptionIdentity(media_source=MediaSource.TMDB, media_id="unknown"),
            SubscriptionPatch({"name": "demo"}),
        )

    unit_of_work.rollback.assert_called_once_with()
    unit_of_work.commit.assert_not_called()


def test_dispatcher_retries_then_dead_letters_with_stable_key() -> None:
    """同一幂等键有限指数退避，达到上限后进入 dead letter。"""
    now = datetime(2026, 8, 21, tzinfo=timezone.utc)
    repository = MagicMock()
    repository.claim.side_effect = [
        ClaimedOutboxMessage(1, "subscribe.added:42:v1", "subscribe.added", {}, 1, 1),
        ClaimedOutboxMessage(1, "subscribe.added:42:v1", "subscribe.added", {}, 1, 2),
    ]
    handler = MagicMock(side_effect=RuntimeError("temporary"))
    failure_observer = MagicMock()
    dispatcher = OutboxDispatcher(
        repository,
        {"subscribe.added": handler},
        max_attempts=2,
        clock=lambda: now,
        failure_observer=failure_observer,
    )

    assert dispatcher.dispatch_one() is True
    assert repository.retry.call_args.kwargs["dead"] is False
    assert dispatcher.dispatch_one() is True
    assert repository.retry.call_args.kwargs["dead"] is True
    assert [call.args[0].event_key for call in handler.call_args_list] == [
        "subscribe.added:42:v1",
        "subscribe.added:42:v1",
    ]
    assert [call.args[0] for call in failure_observer.call_args_list] == [
        False,
        True,
    ]


def test_dispatcher_marks_success_and_closes_owned_resource() -> None:
    """成功 handler 收口消息，批次结束释放 Session 所有权。"""
    now = datetime(2026, 8, 21, tzinfo=timezone.utc)
    repository = MagicMock()
    message = ClaimedOutboxMessage(7, "key", "subscribe.added", {}, 1, 1)
    repository.claim.return_value = message
    close = MagicMock()
    dispatcher = OutboxDispatcher(
        repository,
        {"subscribe.added": MagicMock()},
        clock=lambda: now,
        close=close,
    )

    assert dispatcher.dispatch_one() is True
    repository.complete.assert_called_once_with(7, 1, now)
    dispatcher.close()
    close.assert_called_once_with()


def test_dispatcher_raises_when_complete_loses_lease() -> None:
    """handler 成功但 complete fencing 失败时必须明确报告 lease 丢失。"""
    now = datetime(2026, 8, 28, tzinfo=timezone.utc)
    repository = MagicMock()
    message = ClaimedOutboxMessage(7, "key", "test", {}, 1, 1)
    repository.claim.return_value = message
    repository.complete.return_value = False
    handler = MagicMock()
    dispatcher = OutboxDispatcher(
        repository,
        {"test": handler},
        clock=lambda: now,
    )

    with pytest.raises(OutboxLeaseLostError, match="完成凭证"):
        dispatcher.dispatch_one()

    handler.assert_called_once_with(message)
    repository.retry.assert_not_called()


def test_sync_outbox_claim_is_exclusive_for_event_key() -> None:
    """同步投递与恢复投递竞争同一 intent 时只允许一个取得 lease。"""
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    now = datetime(2026, 8, 24, tzinfo=timezone.utc)
    lease_until = now + timedelta(seconds=60)
    event_key = "subscribe.complete:7:tmdb:123:v1"

    with factory() as session:
        repository = SqlAlchemyOutboxStager(session)
        repository.stage(
            OutboxIntent(event_key=event_key, topic="subscribe.complete", payload={}),
            now,
        )
        session.commit()

    store = SqlAlchemyOutboxDispatchStore(factory)
    owner = store.claim_by_event_key(event_key, now, lease_until)
    competitor = store.claim_by_event_key(event_key, now, lease_until)
    assert owner is not None
    assert owner.attempt == 1
    assert competitor is None

    with factory() as session:
        message = session.execute(select(OutboxMessage)).scalar_one()
    assert message.status == "processing"
    assert message.attempt == 1
    assert message.lease_until == lease_until.isoformat()


def test_concurrent_claim_allows_exactly_one_owner(tmp_path) -> None:
    """两个独立 dispatcher 并发竞争同一消息时只允许一个取得 lease。"""
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'outbox.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    now = datetime(2026, 8, 28, tzinfo=timezone.utc)
    with factory() as session:
        SqlAlchemyOutboxStager(session).stage(
            OutboxIntent(event_key="race:v1", topic="test", payload={}),
            now,
        )
        session.commit()
    barrier = Barrier(2)

    def claim():
        """同时开始一次独立短事务认领。"""
        barrier.wait()
        return SqlAlchemyOutboxDispatchStore(factory).claim_by_event_key(
            "race:v1",
            now,
            now + timedelta(seconds=60),
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        claimed = list(executor.map(lambda _index: claim(), range(2)))

    owners = [message for message in claimed if message is not None]
    assert len(owners) == 1
    assert owners[0].attempt == 1
    engine.dispose()


def test_expired_owner_cannot_settle_new_attempt() -> None:
    """lease 过期后的旧 owner 不得覆盖新 attempt 的完成或重试状态。"""
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    now = datetime(2026, 8, 28, tzinfo=timezone.utc)
    with factory() as session:
        SqlAlchemyOutboxStager(session).stage(
            OutboxIntent(event_key="fenced:v1", topic="test", payload={}),
            now,
        )
        session.commit()
    store = SqlAlchemyOutboxDispatchStore(factory)
    first = store.claim_by_event_key(
        "fenced:v1",
        now,
        now + timedelta(seconds=1),
    )
    second_now = now + timedelta(seconds=2)
    second = store.claim_by_event_key(
        "fenced:v1",
        second_now,
        second_now + timedelta(seconds=60),
    )
    assert first is not None
    assert second is not None
    assert second.attempt == first.attempt + 1

    assert store.complete(first.message_id, first.attempt, second_now) is False
    assert store.retry(
        first.message_id,
        first.attempt,
        next_retry_at=second_now,
        last_error="stale owner",
        dead=False,
    ) is False
    assert store.complete(second.message_id, second.attempt, second_now) is True


def test_handler_replays_with_stable_key_after_success_before_complete_crash() -> None:
    """外部成功后 complete 前崩溃会按稳定键至少再次投递一次。"""
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    now = datetime(2026, 8, 28, tzinfo=timezone.utc)
    event_key = "external-effect:v1"
    with factory() as session:
        SqlAlchemyOutboxStager(session).stage(
            OutboxIntent(event_key=event_key, topic="external", payload={}),
            now,
        )
        session.commit()
    store = SqlAlchemyOutboxDispatchStore(factory)
    first = store.claim(now, now + timedelta(seconds=1))
    assert first is not None
    external_results: list[str] = []

    def handler(message: ClaimedOutboxMessage) -> None:
        """记录 at-least-once 外部效果及其稳定幂等键。"""
        assert message.payload["idempotency_key"] == message.event_key
        external_results.append(message.event_key)

    handler(first)
    dispatcher = OutboxDispatcher(
        store,
        {"external": handler},
        clock=lambda: now + timedelta(seconds=2),
    )

    assert dispatcher.dispatch_one() is True
    assert external_results == [event_key, event_key]
    with factory() as session:
        persisted = session.execute(select(OutboxMessage)).scalar_one()
    assert persisted.status == "completed"
    assert persisted.attempt == 2


@pytest.mark.parametrize("handler_kind", ["event", "notification"])
def test_startup_handler_replays_strict_boundary_with_stable_key(
    handler_kind,
    monkeypatch,
) -> None:
    """真实 startup handler 等待执行边界，并以同一键诚实重放。"""
    from app.command import CommandChain
    from app.runtime.events import EventManager
    from app.startup.initializers.modules import _build_outbox_handlers

    calls = []
    if handler_kind == "event":
        topic = "subscribe.added"
        payload = {"subscribe_id": 7}
        monkeypatch.setattr(
            EventManager,
            "send_event_strict",
            lambda _self, _etype, data: calls.append(data["idempotency_key"]),
        )
    else:
        topic = "subscribe.complete.notification"
        payload = {"message": {"title": "完成", "text": "Test"}}
        monkeypatch.setattr(
            CommandChain,
            "post_message_strict",
            lambda _self, _message, *, event_key: calls.append(event_key),
        )
    handlers = _build_outbox_handlers()
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    now = datetime(2026, 8, 28, tzinfo=timezone.utc)
    event_key = f"startup:{handler_kind}:v1"
    with factory() as session:
        SqlAlchemyOutboxStager(session).stage(
            OutboxIntent(event_key=event_key, topic=topic, payload=payload),
            now,
        )
        session.commit()
    store = SqlAlchemyOutboxDispatchStore(factory)
    first = store.claim(now, now + timedelta(seconds=1))
    assert first is not None
    handlers[topic](first)

    dispatcher = OutboxDispatcher(
        store,
        handlers,
        clock=lambda: now + timedelta(seconds=2),
    )
    assert dispatcher.dispatch_one() is True
    assert calls == [event_key, event_key]


def test_strict_notification_preserves_legacy_provider_signature(monkeypatch) -> None:
    """durable 通知只传既有 message 参数，并在调用上下文携带稳定键。"""
    from app.command import CommandChain
    from app.runtime.correlation import get_correlation_id
    from app.schemas.message import Message

    chain = CommandChain()
    received = []

    def legacy_provider(message) -> None:
        """模拟只接受旧式单参数签名的第三方通知 provider。"""
        received.append((message, get_correlation_id()))

    monkeypatch.setattr(chain.eventmanager, "send_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        chain,
        "run_module_strict",
        lambda method, **kwargs: legacy_provider(**kwargs),
    )

    chain.post_message_strict(
        Message(title="完成", text="Test", save_history=False),
        event_key="subscribe.complete:7:notification",
    )

    assert len(received) == 1
    assert received[0][0].source is None
    assert received[0][1] == "subscribe.complete:7:notification"


def test_strict_notification_retry_writes_history_once(monkeypatch) -> None:
    """provider 失败后按稳定键重试，历史只写一次而渠道继续 at-least-once。"""
    from app.command import CommandChain
    from app.schemas.message import Message

    chain = CommandChain()
    history_sources = set()
    provider_sources = []

    monkeypatch.setattr(chain.eventmanager, "send_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        chain.messageoper,
        "exists_by_source",
        lambda source: source in history_sources,
    )
    monkeypatch.setattr(
        chain.messageoper,
        "add",
        lambda **payload: history_sources.add(payload["source"]),
    )

    def deliver(_method, *, message) -> None:
        """第一次模拟外部失败，第二次成功，并记录 provider 实际路由 source。"""
        provider_sources.append(message.source)
        if len(provider_sources) == 1:
            raise RuntimeError("temporary")

    monkeypatch.setattr(chain, "run_module_strict", deliver)
    message = Message(title="完成", text="Test")

    with pytest.raises(RuntimeError, match="temporary"):
        chain.post_message_strict(message, event_key="subscribe.complete:7:notification")
    chain.post_message_strict(message, event_key="subscribe.complete:7:notification")

    assert history_sources == {"outbox:subscribe.complete:7:notification"}
    assert provider_sources == [None, None]


def test_outbox_cleanup_removes_only_expired_terminal_history_in_batches() -> None:
    """清理只删除超过各自保留期的终态记录，并按批次持续收口。"""
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    now = datetime(2026, 8, 26, tzinfo=timezone.utc)

    def message(
        event_key: str,
        status: str,
        *,
        completed_at: datetime | None = None,
        next_retry_at: datetime | None = None,
    ) -> OutboxMessage:
        """构造指定终态时间的最小 Outbox 测试记录。"""
        return OutboxMessage(
            event_key=event_key,
            topic="test",
            payload_version=1,
            payload={},
            status=status,
            attempt=1,
            next_retry_at=(next_retry_at or now).isoformat(),
            created_at=(now - timedelta(days=120)).isoformat(),
            completed_at=completed_at.isoformat() if completed_at else None,
        )

    with factory() as session:
        session.add_all([
            message(
                "completed-expired-1",
                "completed",
                completed_at=now - timedelta(days=31),
            ),
            message(
                "completed-expired-2",
                "completed",
                completed_at=now - timedelta(days=40),
            ),
            message(
                "completed-boundary",
                "completed",
                completed_at=now - timedelta(days=30),
            ),
            message(
                "dead-expired",
                "dead",
                next_retry_at=now - timedelta(days=91),
            ),
            message(
                "dead-recent",
                "dead",
                next_retry_at=now - timedelta(days=20),
            ),
            message("pending-old", "pending"),
            message("processing-old", "processing"),
        ])
        session.commit()

    cleanup = DataCleanupService(
        repository=DatabaseCleanupRepository(session_factory=factory),
        policy_reader=lambda: CleanupPolicy(
            enabled=True,
            message_days=0,
            download_history_days=0,
            site_userdata_days=0,
            transfer_history_days=0,
            download_failure_days=0,
            subscribe_history_days=0,
            agent_chat_days=0,
            agent_task_run_days=0,
            outbox_completed_days=30,
            outbox_dead_days=90,
        ),
        clock=lambda: now,
    )
    report = cleanup.execute(batch_size=2)

    assert report["tables"]["outbox_completed"]["deleted"] == 2
    assert report["tables"]["outbox_dead"]["deleted"] == 1
    assert report["total_deleted"] == 3

    with factory() as session:
        remaining = set(
            session.execute(select(OutboxMessage.event_key)).scalars().all()
        )
    assert remaining == {
        "completed-boundary",
        "dead-recent",
        "pending-old",
        "processing-old",
    }
