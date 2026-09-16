"""订阅完成应用命令及其同步事务端口。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from app.application.classification.reference import EffectiveClassificationSnapshot
from app.application.outbox import (
    SUBSCRIBE_COMPLETED_TOPIC,
    OutboxDispatchStore,
    OutboxIntent,
    OutboxStager,
    SyncUnitOfWork,
    deliver_outbox_effect,
)
from app.application.subscription.contract import (
    SubscriptionHistoryPatch,
    SubscriptionStagingPort,
)
from app.runtime.log import logger
from app.schemas.common import JsonData

CompletionEffect = Callable[[], None]
CompletionReporter = Callable[[Mapping[str, JsonData]], object]


class CompleteSubscriptionCommand:
    """原子完成订阅，并按通知、事件、统计顺序执行提交后副作用。"""

    def __init__(
        self,
        repository: SubscriptionStagingPort,
        unit_of_work: SyncUnitOfWork,
        outbox: Optional[OutboxStager],
        dispatch_store: Optional[OutboxDispatchStore],
        publish: Callable[[dict[str, JsonData]], None],
    ) -> None:
        """注入共享同步会话、事件发布端口和可选 durable outbox。"""
        self._repository = repository
        self._unit_of_work = unit_of_work
        self._outbox = outbox
        self._dispatch_store = dispatch_store
        self._publish = publish

    def execute(
        self,
        subscribe_id: int,
        subscribe_info: Mapping[str, JsonData],
        mediainfo: Mapping[str, JsonData],
        notify: CompletionEffect,
        report: CompletionReporter,
        notification: Mapping[str, JsonData] | None = None,
        classification_snapshot: EffectiveClassificationSnapshot | None = None,
    ) -> None:
        """在同一事务中写历史、删订阅并暂存完成事件、通知与统计意图。"""
        info = dict(subscribe_info)
        if classification_snapshot is not None:
            info.update(
                {
                    "media_category_id": classification_snapshot.category_id,
                    "media_category": classification_snapshot.path,
                    "classification_rule_id": classification_snapshot.rule_id,
                    "classification_policy_revision": classification_snapshot.policy_revision,
                    "classification_source": classification_snapshot.source,
                }
            )
        occurrence_id = uuid4().hex
        event_key = completion_event_key(
            subscribe_id,
            info,
            occurrence_id=occurrence_id,
        )
        event_payload: dict[str, JsonData] = {
            "subscribe_id": subscribe_id,
            "subscribe_info": info,
            "mediainfo": dict(mediainfo),
            "idempotency_key": event_key,
        }
        report_key = completion_report_key(
            subscribe_id,
            info,
            occurrence_id=occurrence_id,
        )
        notification_key = completion_notification_key(
            subscribe_id,
            info,
            occurrence_id=occurrence_id,
        )
        report_info = _completion_report_payload(info, report_key)
        report_payload: dict[str, JsonData] = {"subscribe_info": report_info}
        try:
            self._repository.stage_history(SubscriptionHistoryPatch.from_subscription(info))
            self._repository.stage_delete_sync(subscribe_id)
            if self._outbox:
                now = datetime.now(timezone.utc)
                self._outbox.stage(
                    OutboxIntent(
                        event_key=event_key,
                        topic=SUBSCRIBE_COMPLETED_TOPIC,
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
            if self._dispatch_store:
                deliver_outbox_effect(
                    self._dispatch_store,
                    notification_key,
                    notify,
                )
            else:
                notify()
        else:
            notify()
        if self._dispatch_store:
            deliver_outbox_effect(
                self._dispatch_store,
                event_key,
                lambda: self._publish(event_payload),
            )
        else:
            self._publish(event_payload)
        if self._dispatch_store:
            try:
                report_delivered = deliver_outbox_effect(
                    self._dispatch_store,
                    report_key,
                    lambda: report(report_info),
                )
            except Exception as error:
                logger.warning(f"订阅完成统计上报失败，将由后台重试：{error}")
            else:
                if report_delivered is False:
                    logger.warning("订阅完成统计上报未确认，将由后台重试")
        else:
            try:
                report(report_info)
            except Exception as error:
                logger.warning(f"订阅完成统计上报失败：{error}")


def completion_event_key(
    subscribe_id: int,
    subscribe_info: Mapping[str, JsonData],
    *,
    occurrence_id: Optional[str] = None,
) -> str:
    """由订阅身份和本次完成事实构造跨重试稳定且不复用的幂等键。"""
    resolved_occurrence_id = occurrence_id or uuid4().hex
    return (
        f"subscribe.complete:{subscribe_id}:"
        f"{subscribe_info.get('media_source') or 'unknown'}:"
        f"{subscribe_info.get('media_id') or 'unknown'}:{resolved_occurrence_id}:v1"
    )


def completion_report_key(
    subscribe_id: int,
    subscribe_info: Mapping[str, JsonData],
    *,
    occurrence_id: Optional[str] = None,
) -> str:
    """构造可独立重试的订阅完成统计幂等键。"""
    event_key = completion_event_key(
        subscribe_id,
        subscribe_info,
        occurrence_id=occurrence_id,
    )
    return f"{event_key}:report"


def completion_notification_key(
    subscribe_id: int,
    subscribe_info: Mapping[str, JsonData],
    *,
    occurrence_id: Optional[str] = None,
) -> str:
    """构造订阅完成通知的稳定幂等键，避免恢复时重复生成不同消息。"""
    event_key = completion_event_key(
        subscribe_id,
        subscribe_info,
        occurrence_id=occurrence_id,
    )
    return f"{event_key}:notification"


def _completion_report_payload(
    subscribe_info: Mapping[str, JsonData],
    report_key: str,
) -> dict[str, JsonData]:
    """保留旧统计接口字段，同时为恢复 handler 固化幂等键。"""
    return {
        "media_source": subscribe_info.get("media_source"),
        "media_id": subscribe_info.get("media_id"),
        "season": subscribe_info.get("season"),
        "idempotency_key": report_key,
    }


CompletionScope = Callable[[], AbstractContextManager[CompleteSubscriptionCommand]]
