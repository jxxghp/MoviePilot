"""
durable side-effect outbox 原子性、认领、重试与稳定重放测试。
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock
from uuid import UUID

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.application.maintenance import CleanupPolicy, DataCleanupService
from app.application.messaging.delivery import NotificationParts, merge_notification_progress
from app.application.outbox import (
    CHANNEL_AUTHORIZATION_RECEIPT_TOPIC,
    DOWNLOAD_MODULE_TOPIC,
    DOWNLOAD_NOTIFICATION_TOPIC,
    DOWNLOAD_SUBTITLE_TOPIC,
    ClaimedOutboxMessage,
    DurableEventCommand,
    OutboxDispatcher,
    OutboxIntent,
    OutboxLeaseLostError,
)
from app.application.subscription.contract import (
    SubscriptionIdentity,
    SubscriptionPatch,
)
from app.application.subscription.write import CreateSubscriptionCommand
from app.chain._messaging import NotificationMixin
from app.db.adapters.outbox import (
    SqlAlchemyAsyncOutboxDispatchStore,
    SqlAlchemyOutboxDispatchStore,
    SqlAlchemyOutboxStager,
)
from app.db.base import Base
from app.db.maintenance import DatabaseCleanupRepository
from app.db.models.outbox import OutboxMessage
from app.db.session import async_session_scope
from app.modules.wechat import WechatModule
from app.modules.wechat.wechat import WeChat
from app.schemas.delivery import DeliveryResult
from app.schemas.message import Message
from app.schemas.system import NotificationConf
from app.schemas.types import MediaSource, NotificationChannel
from app.startup.composition import outbox as outbox_runtime


class _Staged:
    """
    测试用新订阅暂存结果。
    """

    subscribe_id = 42
    message = "ok"
    created = True


def test_subscription_and_outbox_intent_commit_together() -> None:
    """
    业务行与 intent 均 stage 成功后才允许同一次 commit。
    """
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
    key_parts = intent.event_key.split(":")
    assert key_parts[:4] == ["subscribe.added", "42", "unknown", "unknown"]
    assert UUID(key_parts[4]).hex == key_parts[4]
    assert key_parts[5] == "v1"
    assert intent.payload["subscribe_id"] == 42
    report_intent = outbox.stage.call_args_list[1].args[0]
    assert report_intent.topic == "subscribe.added.report"
    assert report_intent.event_key.endswith(":report")


def test_subscription_notification_snapshot_is_part_of_same_transaction() -> None:
    """
    订阅新增通知快照与事件、统计意图一起暂存，便于崩溃恢复。
    """
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
    """
    intent 无法持久化时订阅行不得单独提交。
    """
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
    """
    同一幂等键有限指数退避，达到上限后进入 dead letter。
    """
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
    """
    成功 handler 收口消息，批次结束释放 Session 所有权。
    """
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
    """
    handler 成功但 complete fencing 失败时必须明确报告 lease 丢失。
    """
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
    """
    同步投递与恢复投递竞争同一 intent 时只允许一个取得 lease。
    """
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


@pytest.mark.asyncio
async def test_async_outbox_does_not_claim_absent_event(db):
    """
    工作进程不能为尚未记录的通知分配可发送认领。
    """
    db.watermark(OutboxMessage)
    store = SqlAlchemyAsyncOutboxDispatchStore(async_session_scope)
    now = datetime(2026, 8, 24, tzinfo=timezone.utc)
    assert await store.claim_by_event_key("notification:absent-fixture", now, now + timedelta(seconds=5)) is None


def test_notification_progress_is_fenced_by_claim_and_preserves_confirmed_part(tmp_path) -> None:
    """
    部分投递记录在恢复后可读取，旧认领不能覆盖当前进度。
    """
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'notification.db'}")
    OutboxMessage.__table__.create(engine)
    factory = sessionmaker(engine)
    now = datetime(2026, 8, 24, tzinfo=timezone.utc)
    event_key = "notification:fixture"
    with factory.begin() as session:
        SqlAlchemyOutboxStager(session).stage(OutboxIntent(
            event_key=event_key, topic=DOWNLOAD_NOTIFICATION_TOPIC, payload={"message": {"title": "测试"}},
        ), now)

    store = SqlAlchemyOutboxDispatchStore(factory)
    first = store.claim_by_event_key(event_key, now, now + timedelta(seconds=1))
    assert first is not None
    success = DeliveryResult(channel=NotificationChannel.Wechat, connection_id="corp",
                             identity_namespace="bot", target_id="group:one", part=0, status="confirmed")
    unknown = success.model_copy(update={"part": 1, "status": "unknown"})
    store.update_notification_progress(first, lambda snapshot: (
        None, merge_notification_progress(snapshot, (success, unknown)),
    ))

    later = now + timedelta(seconds=2)
    second = store.claim_by_event_key(event_key, later, later + timedelta(seconds=10))
    assert second is not None
    with pytest.raises(OutboxLeaseLostError):
        store.update_notification_progress(first, lambda _: (None, {}))
    failure = success.model_copy(update={"status": "retryable_failure"})
    store.update_notification_progress(second, lambda snapshot: (
        None, merge_notification_progress(snapshot, (failure,)),
    ))
    with factory() as session:
        row = session.scalars(select(OutboxMessage)).one()
        assert row.payload == {"message": {"title": "测试"}, "idempotency_key": event_key}
        assert row.notification_progress is not None
        assert {(part["part"], part["status"]) for part in row.notification_progress.values()} == {
            (0, "confirmed"), (1, "unknown"),
        }
    engine.dispose()


def test_wechat_app_recovers_unsent_target_after_worker_interruption(tmp_path, monkeypatch) -> None:
    """
    首个连接发送时中断，恢复仍能发送其他原连接，且不加入新的连接。
    """
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'notification-restart.db'}")
    OutboxMessage.__table__.create(engine)
    factory = sessionmaker(engine)
    monkeypatch.setattr(outbox_runtime, "SessionFactory", factory)
    now = datetime(2026, 8, 24, tzinfo=timezone.utc)
    event_key = "notification:restart"
    payload = Message(channel=NotificationChannel.Wechat, userid="alice|bob", text="测试").model_dump(mode="json")
    with factory.begin() as session:
        SqlAlchemyOutboxStager(session).stage(OutboxIntent(
            event_key=event_key, topic=DOWNLOAD_NOTIFICATION_TOPIC, payload={"message": payload},
        ), now)

    config = NotificationConf(id="app", name="app", type="wechat", enabled=True, identity_namespace="app-identity")
    other = config.model_copy(update={"id": "other", "name": "other", "identity_namespace": "other-tenant"})
    configs = {config.name: config, other.name: other}
    monkeypatch.setattr(outbox_runtime, "get_notification_configs", lambda: list(configs.values()))
    module = WechatModule()
    module._channel = NotificationChannel.Wechat
    client = WeChat.__new__(WeChat)
    client._appid = "fixture-app"
    monkeypatch.setattr(module, "get_configs", lambda: configs)
    monkeypatch.setattr(module, "get_config", configs.get)
    monkeypatch.setattr(module, "get_instance", lambda _: client)
    send = Mock(side_effect=[KeyboardInterrupt, ("confirmed", None), ("confirmed", None), ("confirmed", None)])
    monkeypatch.setattr(client, "_send_tracked_part", send)

    def deliver_step(step, **_kwargs):
        """
        以渠道模块替代扩展派发器，保留部分失败时的严格结算语义。
        """
        results = step.message.collect_delivery_results()
        module.post_message(step.message)
        assert {part.connection_id for part in results} <= {"app", "other"}
        if any(item.status != "confirmed" for item in results):
            raise RuntimeError("存在未确认分段")

    def dispatch(message, **_kwargs):
        """
        使用正常严格通知的路由过程，在任何发送前收集全部宿主连接。
        """
        host = SimpleNamespace(
            runtime_config=SimpleNamespace(superuser="admin"),
            prepare_notification_parts=module.post_message,
            _deliver_notification_step=deliver_step,
        )
        NotificationMixin._dispatch_notification_steps(host, message, strict_delivery=True, kwargs={})

    monkeypatch.setattr(outbox_runtime, "CommandChain", lambda: Mock(post_message_strict=dispatch))
    store = SqlAlchemyOutboxDispatchStore(factory)
    first = store.claim_by_event_key(event_key, now, now + timedelta(seconds=1))
    assert first is not None
    with pytest.raises(KeyboardInterrupt):
        outbox_runtime._dispatch_notification(first)
    configs["new"] = config.model_copy(update={"id": "new", "name": "new", "identity_namespace": "second-tenant"})
    later = now + timedelta(seconds=2)
    second = store.claim_by_event_key(event_key, later, later + timedelta(seconds=10))
    assert second is not None
    with pytest.raises(RuntimeError, match="未确认分段"):
        outbox_runtime._dispatch_notification(second)

    assert [call.args[0]["touser"] for call in send.call_args_list] == ["alice", "bob", "alice", "bob"]
    with factory() as session:
        progress = session.scalars(select(OutboxMessage)).one().notification_progress
        assert progress is not None
        assert progress["_frozen_connections"] == [["app", "app-identity"], ["other", "other-tenant"]]
        assert progress["_frozen_delivery_plan"] is True
        assert {(part["connection_id"], part["target_id"]): part["status"]
                for part in progress.values() if isinstance(part, dict)} == {
            ("app", "alice"): "unknown", ("app", "bob"): "confirmed",
            ("other", "alice"): "confirmed", ("other", "bob"): "confirmed",
        }
    engine.dispose()


def test_outbox_cancels_superseded_approval_before_network_submit(tmp_path, monkeypatch) -> None:
    """
    已出队回执在分段提交前复核审批，处理期间撤销旧审批时结算旧意图。
    """
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'authorization-race.db'}")
    OutboxMessage.__table__.create(engine)
    factory = sessionmaker(engine)
    monkeypatch.setattr(outbox_runtime, "SessionFactory", factory)
    now = datetime(2026, 8, 24, tzinfo=timezone.utc)
    key = "receipt:fixture"
    payload = {"member_id": "member", "version": 1.0, "connection_id": "app", "identity_namespace": "tenant"}
    with factory.begin() as session:
        SqlAlchemyOutboxStager(session).stage(OutboxIntent(
            event_key=key, topic=CHANNEL_AUTHORIZATION_RECEIPT_TOPIC, payload=payload,
        ), now)

    conf = NotificationConf(id="app", name="app", type="wechat", enabled=True, identity_namespace="tenant")
    member = SimpleNamespace(channel=NotificationChannel.Wechat.value, sender_id="alice", status="approved")
    service = SimpleNamespace(receipt_target=Mock(side_effect=[(member, "app"), None]))
    monkeypatch.setattr(outbox_runtime, "get_channel_authorization", lambda: service)
    module = WechatModule()
    module._channel = NotificationChannel.Wechat
    client = WeChat.__new__(WeChat)
    client._appid = "fixture-app"
    submit = Mock(return_value=("confirmed", None))
    monkeypatch.setattr(client, "_send_tracked_part", submit)
    monkeypatch.setattr(module, "get_configs", lambda: {"app": conf})
    monkeypatch.setattr(module, "get_config", lambda _: conf)
    monkeypatch.setattr(module, "get_instance", lambda _: client)

    def dispatch(message, **_kwargs):
        """
        经企业微信真实模块触发分段预留和回执复核。
        """
        module.post_message(message)

    monkeypatch.setattr(outbox_runtime, "CommandChain", lambda: Mock(post_message_strict=dispatch))
    store = SqlAlchemyOutboxDispatchStore(factory)
    claimed = store.claim_by_event_key(key, now, now + timedelta(seconds=10))
    assert claimed is not None
    outbox_runtime._dispatch_authorization_receipt(claimed)
    submit.assert_not_called()
    assert service.receipt_target.call_count == 2
    with factory() as session:
        progress = session.scalars(select(OutboxMessage)).one().notification_progress or {}
        assert all(part["code"] == "not_submitted" for part in progress.values() if isinstance(part, dict))
    engine.dispose()


def test_outbox_respects_platform_retry_after(tmp_path, monkeypatch) -> None:
    """
    推送服务明确限流后的等待时间到期前，同一意图不会再次进入发送。
    """
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'webpush-retry.db'}")
    OutboxMessage.__table__.create(engine)
    factory = sessionmaker(engine)
    monkeypatch.setattr(outbox_runtime, "SessionFactory", factory)
    now = datetime(2026, 8, 24, tzinfo=timezone.utc)
    key = "notification:rate-limit"
    payload = Message(channel=NotificationChannel.WebPush, title="完成").model_dump(mode="json")
    with factory.begin() as session:
        SqlAlchemyOutboxStager(session).stage(OutboxIntent(
            event_key=key, topic=DOWNLOAD_NOTIFICATION_TOPIC, payload={"message": payload},
        ), now)
    monkeypatch.setattr(outbox_runtime, "get_notification_configs", lambda: [])

    def dispatch(_message, **_kwargs):
        """
        使用实际分段进度登记一次平台拒绝与等待指示。
        """
        parts = NotificationParts(NotificationChannel.WebPush, "web", "tenant")
        parts.plan((("device", 0, "notification"),))
        parts.send_result("device", 0, "notification", True, lambda: ("retryable_failure", "http_429", None, 17))
        raise RuntimeError("限流")

    monkeypatch.setattr(outbox_runtime, "CommandChain", lambda: Mock(post_message_strict=dispatch))
    store = SqlAlchemyOutboxDispatchStore(factory)
    dispatcher = OutboxDispatcher(store, {DOWNLOAD_NOTIFICATION_TOPIC: outbox_runtime._dispatch_notification},
                                  clock=lambda: now)
    assert dispatcher.dispatch_one() is True
    assert store.claim_by_event_key(key, now + timedelta(seconds=16), now + timedelta(seconds=26)) is None
    claim = store.claim_by_event_key(key, now + timedelta(seconds=17), now + timedelta(seconds=27))
    assert claim is not None and claim.attempt == 2
    engine.dispose()


def test_notification_progress_migration_preserves_legacy_intent_and_guards_downgrade(tmp_path) -> None:
    """
    已有业务通知迁移后保留，写入进度后需要用备份回退。
    """
    from importlib import import_module

    import sqlalchemy as sa
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from pytest import MonkeyPatch

    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'legacy-outbox.db'}")
    migration = import_module("database.versions.62c91ad8b057_channel_resources")
    legacy = sa.Table("outboxmessage", sa.MetaData(),
                      sa.Column("id", sa.Integer, primary_key=True),
                      sa.Column("event_key", sa.String, nullable=False))
    try:
        with engine.begin() as connection, MonkeyPatch.context() as patch:
            legacy.create(connection)
            connection.execute(legacy.insert().values(id=1, event_key="example-event"))
            patch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
            migration._add_notification_progress()
            migration._add_notification_progress()
            current = sa.Table("outboxmessage", sa.MetaData(), autoload_with=connection)
            assert connection.execute(sa.select(current)).one() == (1, "example-event", None)
            with pytest.raises(RuntimeError, match="备份"):
                migration.downgrade()
            migration._add_notification_progress()
            connection.execute(current.update().values(notification_progress={"part": {"status": "confirmed"}}))
            with pytest.raises(RuntimeError, match="备份"):
                migration.downgrade()
    finally:
        engine.dispose()


def test_concurrent_claim_allows_exactly_one_owner(tmp_path) -> None:
    """
    两个独立 dispatcher 并发竞争同一消息时只允许一个取得 lease。
    """
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
        """
        同时开始一次独立短事务认领。
        """
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
    """
    lease 过期后的旧 owner 不得覆盖新 attempt 的完成或重试状态。
    """
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
    """
    外部成功后 complete 前崩溃会按稳定键至少再次投递一次。
    """
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
        """
        记录 at-least-once 外部效果及其稳定幂等键。
        """
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


@pytest.mark.parametrize(
    "handler_kind",
    ["event", "notification", "download_notification", "module", "subtitle"],
)
def test_startup_handler_replays_strict_boundary_with_stable_key(
    handler_kind,
    monkeypatch,
) -> None:
    """
    真实 startup handler 等待执行边界，并以同一键诚实重放。
    """
    from app.command import CommandChain
    from app.runtime.correlation import get_correlation_id
    from app.runtime.events import EventManager
    from app.startup.composition.outbox import build_outbox_handlers

    calls = []
    if handler_kind == "event":
        topic = "subscribe.added"
        payload = {"subscribe_id": 7}
        monkeypatch.setattr(
            EventManager,
            "send_event_strict",
            lambda _self, _etype, data, *, handler_id: calls.append((data["idempotency_key"], handler_id)),
        )
        monkeypatch.setattr(EventManager, "broadcast_handler_ids", lambda _self, _etype: ("test.handler",))
    elif handler_kind in {"notification", "download_notification"}:
        topic = (
            "subscribe.complete.notification"
            if handler_kind == "notification"
            else DOWNLOAD_NOTIFICATION_TOPIC
        )
        payload = {"message": {"title": "完成", "text": "Test"}}
        monkeypatch.setattr(
            CommandChain,
            "post_message_strict",
            lambda _self, _message, *, event_key: calls.append(event_key),
        )
    else:
        import app.chain.download as download_package

        topic = (DOWNLOAD_MODULE_TOPIC if handler_kind == "module" else DOWNLOAD_SUBTITLE_TOPIC)
        payload = {
            "context": {},
            "download_dir": "/downloads",
            "torrent_content": {"kind": "bytes", "value": "dG9ycmVudA=="},
        }
        chain = MagicMock()
        method = (
            chain.download_added
            if handler_kind == "module"
            else chain.download_site_subtitles
        )
        method.side_effect = lambda **_kwargs: calls.append(get_correlation_id())
        monkeypatch.setattr(download_package, "DownloadChain", lambda: chain)
    handlers = build_outbox_handlers()
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    if handler_kind in {"event", "notification", "download_notification"}:
        from app.startup.composition import outbox as composition

        monkeypatch.setattr(composition, "SessionFactory", factory)
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
    if handler_kind == "event":
        from app.startup.composition.outbox import dispatch_event_handler

        current = datetime.now(timezone.utc)
        child = store.claim(current, current + timedelta(seconds=60))
        assert child is not None
        dispatch_event_handler(child)
        assert calls == [(event_key, "test.handler")]
        assert store.complete(child.message_id, child.attempt, current)
        assert store.claim(current, current + timedelta(seconds=60)) is None
    else:
        assert calls == [event_key, event_key]


def test_download_writer_contract_has_no_anonymous_after_commit_path() -> None:
    """
    下载 durable 写端口只接受可持久化具名效果，不再接受匿名回调。
    """
    import inspect

    from app.application.chain.events import ChainDurableEventWriter

    assert "after_commit" not in inspect.signature(
        ChainDurableEventWriter.download_added
    ).parameters
    assert "after_commit" not in inspect.signature(
        DurableEventCommand.execute
    ).parameters


def test_outbox_handler_builder_keeps_runtime_owners_lazy(monkeypatch) -> None:
    """
    构造 handler 映射时先校验完整性，且不提前实例化运行 owner。
    """
    import app.startup.composition.outbox as outbox_composition

    command_chain = MagicMock(side_effect=AssertionError("CommandChain 不应提前实例化"))
    event_manager = MagicMock(side_effect=AssertionError("EventManager 不应提前实例化"))
    validate_handlers = MagicMock()
    monkeypatch.setattr(outbox_composition, "CommandChain", command_chain)
    monkeypatch.setattr(outbox_composition, "EventManager", event_manager)
    monkeypatch.setattr(
        outbox_composition,
        "validate_durable_event_handlers",
        validate_handlers,
    )

    handlers = outbox_composition.build_outbox_handlers()

    assert handlers
    validate_handlers.assert_called_once_with(handlers)
    command_chain.assert_not_called()
    event_manager.assert_not_called()


def test_strict_notification_preserves_legacy_provider_signature(monkeypatch) -> None:
    """
    durable 通知只传既有 message 参数，并在调用上下文携带稳定键。
    """
    from app.command import CommandChain
    from app.runtime.correlation import get_correlation_id
    from app.schemas.message import Message

    chain = CommandChain()
    received = []

    monkeypatch.setattr(
        chain.user_repository,
        "get_notification_settings",
        lambda _username: {"telegram_userid": "admin-1"},
    )
    def legacy_provider(message) -> None:
        """
        模拟只接受旧式单参数签名的第三方通知 provider。
        """
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


def test_managed_notification_retries_host_without_repeating_legacy_handlers(tmp_path, monkeypatch) -> None:
    """
    旧插件和通知事件只走一次，宿主失败分段仍能从数据库认领并重试。
    """
    from app.command import CommandChain

    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'legacy-notice.db'}")
    try:
        OutboxMessage.__table__.create(engine)
        factory = sessionmaker(engine)
        monkeypatch.setattr(outbox_runtime, "SessionFactory", factory)
        now = datetime(2026, 8, 24, tzinfo=timezone.utc)
        intent = OutboxIntent(
            event_key="notifications:legacy-once", topic=DOWNLOAD_NOTIFICATION_TOPIC,
            payload={"message": Message(title="完成", text="通知", save_history=False).model_dump(mode="json")},
        )
        with factory.begin() as session:
            SqlAlchemyOutboxStager(session).stage(intent, now)

        chain = CommandChain()
        notices = Mock()
        monkeypatch.setattr(chain.eventmanager, "send_event", notices)
        monkeypatch.setattr(chain.user_repository, "get_notification_settings", lambda _username: {"telegram_userid": "42"})
        config = NotificationConf(id="tg", name="tg", type="telegram", enabled=True, identity_namespace="tg")
        monkeypatch.setattr(outbox_runtime, "get_notification_configs", lambda: [config])
        monkeypatch.setattr(outbox_runtime, "CommandChain", lambda: chain)
        parts = NotificationParts(NotificationChannel.Telegram, "tg", "tg")
        target = "42"
        monkeypatch.setattr(chain, "prepare_notification_parts", lambda _message: parts.plan(((target, 0, "正文"),)))
        submit = Mock(side_effect=[("retryable_failure", "rate_limited"), ("confirmed", None)])

        def host(message):
            """
            经过正常分段认领提交模拟平台请求，保留实际结果。
            """
            status, code = parts.send(target, 0, "正文", True, submit)
            parts.record(message, target, 0, status, code)

        combined = Mock(side_effect=lambda _method, message: host(message))
        host_only = Mock(side_effect=host)
        monkeypatch.setattr(chain, "run_module_strict", combined)
        monkeypatch.setattr(chain, "deliver_host_notification", host_only)
        store = SqlAlchemyOutboxDispatchStore(factory)
        first = store.claim_by_event_key(intent.event_key, now, now + timedelta(seconds=1))
        assert first is not None
        with pytest.raises(RuntimeError, match="未确认"):
            outbox_runtime._dispatch_notification(first)
        later = now + timedelta(seconds=2)
        second = store.claim_by_event_key(intent.event_key, later, later + timedelta(seconds=10))
        assert second is not None
        outbox_runtime._dispatch_notification(second)
        combined.assert_called_once()
        host_only.assert_called_once()
        notices.assert_called_once()
        assert submit.call_count == 2
        with factory() as session:
            progress = session.scalars(select(OutboxMessage)).one().notification_progress
            assert progress["_legacy_notice_claimed"] is True
            assert [part["status"] for part in progress.values() if isinstance(part, dict)] == ["confirmed"]
    finally:
        engine.dispose()


def test_strict_notification_retry_writes_history_once(monkeypatch) -> None:
    """
    provider 失败后按稳定键重试，历史只写一次而渠道继续 at-least-once。
    """
    from app.command import CommandChain
    from app.schemas.message import Message

    chain = CommandChain()
    history_sources = set()
    provider_sources = []

    monkeypatch.setattr(
        chain.user_repository,
        "get_notification_settings",
        lambda _username: {"telegram_userid": "admin-1"},
    )
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
        """
        第一次模拟外部失败，第二次成功，并记录 provider 实际路由 source。
        """
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
    """
    清理只删除超过各自保留期的终态记录，并按批次持续收口。
    """
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
        """
        构造指定终态时间的最小 Outbox 测试记录。
        """
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
