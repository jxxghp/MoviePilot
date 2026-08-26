"""durable side-effect outbox 原子性、认领、重试与幂等测试。"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.application.maintenance import CleanupPolicy, DataCleanupService
from app.application.outbox import ClaimedOutboxMessage, OutboxDispatcher, OutboxIntent
from app.application.subscription.write import CreateSubscriptionCommand
from app.db.adapters.outbox import SqlAlchemyOutboxRepository
from app.db.base import Base
from app.db.maintenance import DatabaseCleanupRepository
from app.db.models.outbox import OutboxMessage


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

    result = command.execute({}, {"name": "demo"}, "user")

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
        {},
        {"name": "demo"},
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
        command.execute({}, {"name": "demo"})

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
    repository.complete.assert_called_once_with(7, now)
    dispatcher.close()
    close.assert_called_once_with()


def test_sync_outbox_claim_is_exclusive_for_event_key() -> None:
    """同步投递与恢复投递竞争同一 intent 时只允许一个取得 lease。"""
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    now = datetime(2026, 8, 24, tzinfo=timezone.utc)
    lease_until = now + timedelta(seconds=60)
    event_key = "subscribe.complete:7:tmdb:123:v1"

    with factory() as session:
        repository = SqlAlchemyOutboxRepository(session)
        repository.stage(
            OutboxIntent(event_key=event_key, topic="subscribe.complete", payload={}),
            now,
        )
        session.commit()

    with factory() as owner, factory() as competitor:
        assert SqlAlchemyOutboxRepository(owner).claim_by_event_key(
            event_key, now, lease_until
        ) is True
        assert SqlAlchemyOutboxRepository(competitor).claim_by_event_key(
            event_key, now, lease_until
        ) is False

    with factory() as session:
        message = session.execute(select(OutboxMessage)).scalar_one()
    assert message.status == "processing"
    assert message.attempt == 1
    assert message.lease_until == lease_until.isoformat()


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
