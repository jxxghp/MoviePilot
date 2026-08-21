"""按媒体身份批量删除订阅的应用用例。"""

from datetime import datetime, timezone
from typing import Any, Callable, Protocol

from app.application.outbox import AsyncOutboxTransaction, OutboxIntent
from app.application.subscription.delete import (
    AsyncUnitOfWork,
    SubscribeDeletedPublisher,
    SubscribeDeletionActor,
    SubscribeDeletionCandidate,
    build_subscribe_deleted_payload,
)
from app.schemas.types import MediaSource


class SubscribeIdentityDeletionRepository(Protocol):
    """按媒体身份删除订阅所需的数据访问端口。"""

    async def list_candidates_by_identity(
        self,
        media_source: MediaSource,
        media_id: str,
        season: int | None,
        music_type: str | None,
    ) -> list[SubscribeDeletionCandidate]:
        """读取匹配媒体身份的去重订阅快照。"""
        ...

    async def stage_delete(self, subscribe_id: int) -> None:
        """把指定订阅登记为待删除，但不自行提交事务。"""
        ...


SubscribeDeletionEventErrorHandler = Callable[[int, Exception], None]


class DeleteSubscriptionsByIdentityCommand:
    """按媒体身份删除当前用户可访问的全部订阅。"""

    def __init__(
        self,
        repository: SubscribeIdentityDeletionRepository,
        unit_of_work: AsyncUnitOfWork,
        publish_deleted: SubscribeDeletedPublisher,
        handle_event_error: SubscribeDeletionEventErrorHandler,
        outbox: AsyncOutboxTransaction | None = None,
    ) -> None:
        """注入数据访问、事务、事件和事件错误处理端口。"""
        self._repository = repository
        self._unit_of_work = unit_of_work
        self._publish_deleted = publish_deleted
        self._handle_event_error = handle_event_error
        self._outbox = outbox

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
            media_source,
            media_id,
            season,
            music_type,
        )
        deletions = [
            candidate
            for candidate in candidates
            if self._can_delete(candidate, actor)
        ]
        events: list[tuple[SubscribeDeletionCandidate, dict[str, Any]]] = []
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
                    await self._outbox.stage(
                        OutboxIntent(
                            event_key=event_payload["idempotency_key"],
                            topic="subscribe.deleted",
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
                await self._publish_deleted(event_payload)
                if self._outbox:
                    await self._outbox.complete_by_event_key(
                        event_payload["idempotency_key"],
                        datetime.now(timezone.utc),
                    )
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
