"""订阅完成应用命令及其同步事务端口。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from app.application.outbox import (
    OUTBOX_LEASE_SECONDS,
    OutboxIntent,
    SyncOutboxTransaction,
    SyncUnitOfWork,
)


class SubscriptionCompletionRepository(Protocol):
    """订阅完成命令需要的最小同步持久化端口。"""

    def add_history(self, **payload: Any) -> None:
        """在当前事务中暂存订阅历史。"""
        ...

    def delete(self, subscribe_id: int) -> None:
        """在当前事务中暂存订阅删除。"""
        ...


CompletionEffect = Callable[[], None]
CompletionReporter = Callable[[Mapping[str, Any]], object]


class CompleteSubscriptionCommand:
    """原子完成订阅，并按通知、事件、统计顺序执行提交后副作用。"""

    def __init__(
        self,
        repository: SubscriptionCompletionRepository,
        unit_of_work: SyncUnitOfWork,
        outbox: SyncOutboxTransaction | None,
        publish: Callable[[dict[str, Any]], None],
    ) -> None:
        """注入共享同步会话、事件发布端口和可选 durable outbox。"""
        self._repository = repository
        self._unit_of_work = unit_of_work
        self._outbox = outbox
        self._publish = publish

    def execute(
        self,
        subscribe_id: int,
        subscribe_info: Mapping[str, Any],
        mediainfo: Mapping[str, Any],
        notify: CompletionEffect,
        report: CompletionReporter,
        notification: Mapping[str, Any] | None = None,
    ) -> None:
        """在同一事务中写历史、删订阅并暂存完成事件、通知与统计意图。"""
        info = dict(subscribe_info)
        event_payload = {
            "subscribe_id": subscribe_id,
            "subscribe_info": info,
            "mediainfo": dict(mediainfo),
            "idempotency_key": completion_event_key(subscribe_id, info),
        }
        event_key = event_payload["idempotency_key"]
        report_key = completion_report_key(subscribe_id, info)
        notification_key = completion_notification_key(subscribe_id, info)
        report_payload = {"subscribe_info": _completion_report_payload(info, report_key)}
        try:
            self._repository.add_history(**info)
            self._repository.delete(subscribe_id)
            if self._outbox:
                now = datetime.now(timezone.utc)
                self._outbox.stage(
                    OutboxIntent(
                        event_key=event_key,
                        topic="subscribe.complete",
                        payload=event_payload,
                    ),
                    now,
                )
                if notification:
                    self._outbox.stage(
                        OutboxIntent(
                            event_key=notification_key,
                            topic="subscribe.complete.notification",
                            payload={
                                "idempotency_key": notification_key,
                                "message": dict(notification),
                            },
                        ),
                        now,
                    )
                self._outbox.stage(
                    OutboxIntent(
                        event_key=report_key,
                        topic="subscribe.complete.report",
                        payload=report_payload,
                    ),
                    now,
                )
            self._unit_of_work.commit()
        except Exception:
            self._unit_of_work.rollback()
            raise

        if notification:
            if self._claim_sync_delivery(notification_key):
                notify()
                self._complete_sync_delivery(notification_key)
        else:
            notify()
        if self._claim_sync_delivery(event_key):
            self._publish(event_payload)
            self._complete_sync_delivery(event_key)
        if self._claim_sync_delivery(report_key):
            if report(report_payload["subscribe_info"]) is False:
                raise RuntimeError("订阅完成统计上报未确认")
            self._complete_sync_delivery(report_key)

    def _claim_sync_delivery(self, event_key: str) -> bool:
        """在同步副作用前取得 lease，已由恢复投递接管时跳过直投。"""
        if self._outbox is None:
            return True
        now = datetime.now(timezone.utc)
        return self._outbox.claim_by_event_key(
            event_key,
            now,
            now + timedelta(seconds=OUTBOX_LEASE_SECONDS),
        )

    def _complete_sync_delivery(self, event_key: str) -> None:
        """收口当前同步投递持有的 durable intent。"""
        if self._outbox:
            self._outbox.complete_by_event_key(event_key, datetime.now(timezone.utc))


def completion_event_key(subscribe_id: int, subscribe_info: Mapping[str, Any]) -> str:
    """构造跨重试稳定的订阅完成事件幂等键。"""
    return (
        f"subscribe.complete:{subscribe_id}:"
        f"{subscribe_info.get('media_source') or 'unknown'}:"
        f"{subscribe_info.get('media_id') or 'unknown'}:v1"
    )


def completion_report_key(subscribe_id: int, subscribe_info: Mapping[str, Any]) -> str:
    """构造可独立重试的订阅完成统计幂等键。"""
    return f"{completion_event_key(subscribe_id, subscribe_info)}:report"


def completion_notification_key(
    subscribe_id: int,
    subscribe_info: Mapping[str, Any],
) -> str:
    """构造订阅完成通知的稳定幂等键，避免恢复时重复生成不同消息。"""
    return f"{completion_event_key(subscribe_id, subscribe_info)}:notification"


def _completion_report_payload(
    subscribe_info: Mapping[str, Any],
    report_key: str,
) -> dict[str, Any]:
    """保留旧统计接口字段，同时为恢复 handler 固化幂等键。"""
    return {
        "media_source": subscribe_info.get("media_source"),
        "media_id": subscribe_info.get("media_id"),
        "season": subscribe_info.get("season"),
        "idempotency_key": report_key,
    }


CompletionScope = Callable[[], AbstractContextManager[CompleteSubscriptionCommand]]
_configured_completion_scope: CompletionScope | None = None


def configure_subscription_completion_scope(provider: CompletionScope) -> None:
    """由启动组合根登记订阅完成独占事务作用域。"""
    global _configured_completion_scope
    _configured_completion_scope = provider


def get_subscription_completion_scope() -> AbstractContextManager[CompleteSubscriptionCommand]:
    """返回一次独占同步订阅完成事务作用域。"""
    if _configured_completion_scope is None:
        raise RuntimeError("订阅完成事务作用域尚未配置")
    return _configured_completion_scope()
