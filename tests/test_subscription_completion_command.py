"""订阅完成命令的原子写入、时序与 durable intent 测试。"""

from datetime import datetime

import pytest

from app.application.classification.reference import EffectiveClassificationSnapshot
from app.application.outbox import ClaimedOutboxMessage
from app.application.subscription.complete import CompleteSubscriptionCommand
from app.application.subscription.contract import SubscriptionHistoryPatch


class _Repository:
    """记录历史暂存和订阅删除顺序。"""

    def __init__(self, calls: list[tuple]) -> None:
        """保存共享调用序列。"""
        self.calls = calls

    def stage_history(self, payload: SubscriptionHistoryPatch) -> None:
        """记录历史快照。"""
        self.calls.append(("history", payload.to_payload()))

    def stage_delete_sync(self, subscribe_id: int) -> None:
        """记录待删除订阅。"""
        self.calls.append(("delete", subscribe_id))


class _UnitOfWork:
    """记录提交和回滚。"""

    def __init__(self, calls: list[tuple], error: Exception | None = None) -> None:
        """保存调用序列与可选提交异常。"""
        self.calls = calls
        self.error = error

    def commit(self) -> None:
        """记录提交并按需失败。"""
        self.calls.append(("commit",))
        if self.error:
            raise self.error

    def rollback(self) -> None:
        """记录回滚。"""
        self.calls.append(("rollback",))


class _Outbox:
    """记录 intent 暂存与即时收口。"""

    def __init__(self, calls: list[tuple], claim_result: bool = True) -> None:
        """保存共享调用序列。"""
        self.calls = calls
        self.claim_result = claim_result

    def stage(self, intent, _now: datetime) -> None:
        """记录 durable intent。"""
        self.calls.append(("stage", intent))

    def claim_by_event_key(self, event_key: str, _now: datetime, _lease_until: datetime):
        """记录同步投递认领结果。"""
        self.calls.append(("claim", event_key))
        if not self.claim_result:
            return None
        return ClaimedOutboxMessage(
            message_id=len(self.calls),
            event_key=event_key,
            topic="test",
            payload={},
            payload_version=1,
            attempt=1,
        )

    def complete(self, message_id: int, attempt: int, _now: datetime) -> bool:
        """记录成功副作用对应的 intent 收口。"""
        self.calls.append(("complete", message_id, attempt))
        return True

    def retry(self, message_id: int, attempt: int, **_kwargs) -> bool:
        """记录失败副作用对应的 intent 释放。"""
        self.calls.append(("retry", message_id, attempt))
        return True


def _command(
    calls: list[tuple],
    *,
    publish_error=None,
    report_result=True,
    notify_error=None,
    claim_result=True,
    report_error=None,
):
    """构造可注入失败的完成命令。"""

    def notify() -> None:
        """记录通知。"""
        calls.append(("notify",))
        if notify_error:
            raise notify_error

    def publish(payload) -> None:
        """记录完成事件。"""
        calls.append(("event", payload))
        if publish_error:
            raise publish_error

    def report(payload) -> bool:
        """记录完成统计。"""
        calls.append(("report", payload))
        if report_error:
            raise report_error
        return report_result

    outbox = _Outbox(calls, claim_result)
    return (
        CompleteSubscriptionCommand(
            repository=_Repository(calls),
            unit_of_work=_UnitOfWork(calls),
            outbox=outbox,
            dispatch_store=outbox,
            publish=publish,
        ),
        notify,
        report,
    )


@pytest.mark.parametrize("failure", ["event", "notify"])
def test_completion_stages_business_and_independent_intents_before_commit(failure):
    """完成事务先提交业务和两个 intent，提交后按通知、事件、统计顺序执行。"""
    calls = []
    command, notify, report = _command(
        calls,
        publish_error=RuntimeError("event failed") if failure == "event" else None,
        notify_error=RuntimeError("notify failed") if failure == "notify" else None,
    )

    with pytest.raises(RuntimeError):
        command.execute(
            7,
            {"id": 7, "media_source": "tmdb", "media_id": "123", "season": 2},
            {"title": "Test"},
            notify=notify,
            report=report,
        )

    assert [call[0] for call in calls[:5]] == [
        "history",
        "delete",
        "stage",
        "stage",
        "commit",
    ]
    assert calls[2][1].topic == "subscribe.complete"
    assert calls[3][1].topic == "subscribe.complete.report"
    if failure == "notify":
        assert [call[0] for call in calls[5:]] == ["notify"]
    elif failure == "event":
        assert [call[0] for call in calls[5:]] == [
            "notify",
            "claim",
            "event",
            "retry",
        ]


def test_completion_report_failure_returns_success_and_keeps_intent_pending():
    """统计未确认不得误报完成失败，且 report intent 必须留待重试。"""
    calls = []
    command, notify, report = _command(calls, report_result=False)

    command.execute(
        7,
        {"id": 7, "media_source": "tmdb", "media_id": "123", "season": 2},
        {"title": "Test"},
        notify=notify,
        report=report,
    )

    assert [call[0] for call in calls] == [
        "history",
        "delete",
        "stage",
        "stage",
        "commit",
        "notify",
        "claim",
        "event",
        "complete",
        "claim",
        "report",
        "retry",
    ]


def test_completion_history_uses_actual_effective_classification_snapshot() -> None:
    """完成历史必须记录本次执行结果，而不是活动订阅创建时的旧路径。"""
    calls = []
    command, notify, report = _command(calls)

    command.execute(
        7,
        {
            "id": 7,
            "name": "Test",
            "media_category_id": "movie.stale",
            "media_category": "旧路径",
        },
        {"title": "Test"},
        notify=notify,
        report=report,
        classification_snapshot=EffectiveClassificationSnapshot(
            category_id="movie.actual",
            category_path=("电影", "剧情"),
            rule_id="rule.drama",
            policy_revision=11,
            source="automatic",
        ),
    )

    history = next(call[1] for call in calls if call[0] == "history")
    assert history["media_category_id"] == "movie.actual"
    assert history["media_category"] == "电影/剧情"
    assert history["classification_rule_id"] == "rule.drama"
    assert history["classification_policy_revision"] == 11
    assert history["classification_source"] == "automatic"


def test_completion_report_error_returns_success_and_keeps_intent_pending():
    """统计上报抛出异常也不得覆盖已经成功提交的完成结果。"""
    calls = []
    command, notify, report = _command(
        calls,
        report_error=RuntimeError("remote failed"),
    )

    command.execute(
        7,
        {"id": 7, "media_source": "tmdb", "media_id": "123", "season": 2},
        {"title": "Test"},
        notify=notify,
        report=report,
    )

    assert [call[0] for call in calls] == [
        "history",
        "delete",
        "stage",
        "stage",
        "commit",
        "notify",
        "claim",
        "event",
        "complete",
        "claim",
        "report",
        "retry",
    ]


def test_completion_success_closes_event_then_report_intent():
    """成功完成按兼容顺序通知、事件、统计，并分别收口两个 intent。"""
    calls = []
    command, notify, report = _command(calls)

    command.execute(
        7,
        {"id": 7, "media_source": "tmdb", "media_id": "123", "season": 2},
        {"title": "Test"},
        notify=notify,
        report=report,
    )

    assert [call[0] for call in calls] == [
        "history",
        "delete",
        "stage",
        "stage",
        "commit",
        "notify",
        "claim",
        "event",
        "complete",
        "claim",
        "report",
        "complete",
    ]
    assert calls[7][1]["idempotency_key"] == calls[2][1].event_key
    assert calls[10][1]["idempotency_key"] == calls[3][1].event_key


def test_completion_keys_distinguish_reused_subscribe_id() -> None:
    """同一订阅主键被复用时，完成事件与旧 Outbox 历史保持不同。"""
    first_calls: list[tuple] = []
    first_command, first_notify, first_report = _command(first_calls)
    first_command.execute(
        7,
        {"id": 7, "media_source": "tmdb", "media_id": "123", "season": 2},
        {"title": "Test"},
        notify=first_notify,
        report=first_report,
    )

    second_calls: list[tuple] = []
    second_command, second_notify, second_report = _command(second_calls)
    second_command.execute(
        7,
        {"id": 7, "media_source": "tmdb", "media_id": "123", "season": 2},
        {"title": "Test"},
        notify=second_notify,
        report=second_report,
    )

    first_event = next(call[1] for call in first_calls if call[0] == "stage")
    second_event = next(call[1] for call in second_calls if call[0] == "stage")
    assert first_event.event_key != second_event.event_key


def test_completion_stages_and_closes_notification_snapshot() -> None:
    """完成通知快照与业务事务同提交，成功即时投递后独立收口。"""
    calls = []
    command, notify, report = _command(calls)

    command.execute(
        7,
        {"id": 7, "media_source": "tmdb", "media_id": "123", "season": 2},
        {"title": "Test"},
        notify=notify,
        report=report,
        notification={"title": "完成", "text": "Test"},
    )

    staged = [call[1] for call in calls if call[0] == "stage"]
    assert [intent.topic for intent in staged] == [
        "subscribe.complete",
        "subscribe.complete.notification",
        "subscribe.complete.report",
    ]
    assert staged[1].payload["message"]["title"] == "完成"
    completed = [call for call in calls if call[0] == "complete"]
    notification_claim = next(call for call in calls if call[0] == "claim" and call[1].endswith(":notification"))
    assert completed[0][1] > 0
    assert notification_claim[1].endswith(":notification")


def test_completion_skips_sync_delivery_owned_by_outbox_dispatcher() -> None:
    """后台已认领 intent 时同步路径不得再次发送相同副作用。"""
    calls = []
    command, notify, report = _command(calls, claim_result=False)

    command.execute(
        7,
        {"id": 7, "media_source": "tmdb", "media_id": "123", "season": 2},
        {"title": "Test"},
        notify=notify,
        report=report,
        notification={"title": "完成", "text": "Test"},
    )

    assert [call[0] for call in calls] == [
        "history",
        "delete",
        "stage",
        "stage",
        "stage",
        "commit",
        "claim",
        "claim",
        "claim",
    ]
