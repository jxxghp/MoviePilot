"""按媒体身份批量删除订阅的应用用例。"""

from datetime import datetime, timezone
from typing import Callable, Optional, cast

from app.application.outbox import (
    SUBSCRIBE_DELETED_TOPIC,
    AsyncOutboxDispatchStore,
    AsyncOutboxStager,
    OutboxIntent,
    deliver_async_outbox_effect,
)
from app.application.subscription.contract import (
    SubscribeDeletionCandidate,
    SubscriptionIdentity,
    SubscriptionStagingPort,
)
from app.application.subscription.delete import (
    AsyncUnitOfWork,
    SubscribeDeletedPublisher,
    SubscribeDeletionActor,
    build_subscribe_deleted_payload,
)
from app.schemas.common import JsonData
from app.schemas.types import MediaSource

SubscribeDeletionEventErrorHandler = Callable[[int, Exception], None]


class DeleteSubscriptionsByIdentityCommand:
    """按媒体身份删除当前用户可访问的全部订阅。"""

    def __init__(
        self,
        repository: SubscriptionStagingPort,
        unit_of_work: AsyncUnitOfWork,
        publish_deleted: SubscribeDeletedPublisher,
        handle_event_error: SubscribeDeletionEventErrorHandler,
        outbox: Optional[AsyncOutboxStager] = None,
        dispatch_store: Optional[AsyncOutboxDispatchStore] = None,
    ) -> None:
        """注入数据访问、事务、事件和事件错误处理端口。"""
        self._repository = repository
        self._unit_of_work = unit_of_work
        self._publish_deleted = publish_deleted
        self._handle_event_error = handle_event_error
        self._outbox = outbox
        self._dispatch_store = dispatch_store

    async def execute(
        self,
        media_source: MediaSource,
        media_id: str,
        season: int | None,
        music_type: str | None,
        actor: SubscribeDeletionActor,
    ) -> int:
        """删除匹配订阅，并在提交后逐条发送兼容事件。"""
        candidates = await self._repository.list_candidates_by_identity(
            SubscriptionIdentity(
                media_source=media_source,
                media_id=media_id,
                season=season,
                music_type=music_type,
            )
        )
        deletions = [candidate for candidate in candidates if self._can_delete(candidate, actor)]
        events: list[tuple[SubscribeDeletionCandidate, dict[str, JsonData]]] = []
        for candidate in deletions:
            await self._repository.stage_delete(candidate.subscribe_id)
            event_payload = build_subscribe_deleted_payload(
                candidate.subscribe_id,
                candidate.event_payload,
            )
            events.append((candidate, event_payload))

        try:
            if self._outbox:
                now = datetime.now(timezone.utc)
                for _, event_payload in events:
                    event_key = cast(str, event_payload["idempotency_key"])
                    await self._outbox.stage(
                        OutboxIntent(
                            event_key=event_key,
                            topic=SUBSCRIBE_DELETED_TOPIC,
                            payload=event_payload,
                        ),
                        now,
                    )
            await self._unit_of_work.commit()
        except Exception:
            await self._unit_of_work.rollback()
            raise

        for candidate, event_payload in events:
            try:
                if self._dispatch_store:
                    event_key = cast(str, event_payload["idempotency_key"])

                    async def publish_event(
                        payload: dict[str, JsonData] = event_payload,
                    ) -> None:
                        """发布当前删除候选对应的稳定事件快照。"""
                        await self._publish_deleted(payload)

                    await deliver_async_outbox_effect(
                        self._dispatch_store,
                        event_key,
                        publish_event,
                    )
                else:
                    await self._publish_deleted(event_payload)
            except Exception as error:
                self._handle_event_error(candidate.subscribe_id, error)
        return len(deletions)

    @staticmethod
    def _can_delete(
        candidate: SubscribeDeletionCandidate,
        actor: SubscribeDeletionActor,
    ) -> bool:
        """判断用户是否拥有候选订阅的删除权限。"""
        if actor.is_superuser:
            return True
        return bool(candidate.username) and candidate.username == actor.username
