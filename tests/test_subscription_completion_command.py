"""订阅完成命令的原子写入、时序与 durable intent 测试。"""

from datetime import datetime

import pytest

from app.application.subscription.complete import CompleteSubscriptionCommand


class _Repository:
    """记录历史暂存和订阅删除顺序。"""

    def __init__(self, calls: list[tuple]) -> None:
        """保存共享调用序列。"""
        self.calls = calls

    def add_history(self, **payload) -> None:
        """记录历史快照。"""
        self.calls.append(("history", payload))

    def delete(self, subscribe_id: int) -> None:
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

    def claim_by_event_key(self, event_key: str, _now: datetime, _lease_until: datetime) -> bool:
        """记录同步投递认领结果。"""
        self.calls.append(("claim", event_key))
        return self.claim_result

    def complete_by_event_key(self, event_key: str, _now: datetime) -> None:
        """记录成功副作用对应的 intent 收口。"""
        self.calls.append(("complete", event_key))


def _command(
    calls: list[tuple],
    *,
    publish_error=None,
    report_result=True,
    notify_error=None,
    claim_result=True,
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
        return report_result

    return CompleteSubscriptionCommand(
        repository=_Repository(calls),
        unit_of_work=_UnitOfWork(calls),
        outbox=_Outbox(calls, claim_result),
        publish=publish,
    ), notify, report


@pytest.mark.parametrize("failure", ["event", "report", "notify"])
def test_completion_stages_business_and_independent_intents_before_commit(failure):
    """完成事务先提交业务和两个 intent，提交后按通知、事件、统计顺序执行。"""
    calls = []
    command, notify, report = _command(
        calls,
        publish_error=RuntimeError("event failed") if failure == "event" else None,
        report_result=False if failure == "report" else True,
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
        "history", "delete", "stage", "stage", "commit",
    ]
    assert calls[2][1].topic == "subscribe.complete"
    assert calls[3][1].topic == "subscribe.complete.report"
    if failure == "notify":
        assert [call[0] for call in calls[5:]] == ["notify"]
    elif failure == "event":
        assert [call[0] for call in calls[5:]] == ["notify", "claim", "event"]
    else:
        assert [call[0] for call in calls[5:]] == [
            "notify", "claim", "event", "complete", "claim", "report",
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
        "history", "delete", "stage", "stage", "commit",
        "notify", "claim", "event", "complete",
        "claim", "report", "complete",
    ]
    assert calls[7][1]["idempotency_key"] == calls[2][1].event_key
    assert calls[10][1]["idempotency_key"] == calls[3][1].event_key


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
    completed = [call[1] for call in calls if call[0] == "complete"]
    assert completed[0].endswith(":notification")


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
        "history", "delete", "stage", "stage", "stage", "commit",
        "claim", "claim", "claim",
    ]
