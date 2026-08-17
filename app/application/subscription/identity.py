"""按媒体身份批量删除订阅的应用用例。"""

from typing import Callable, Protocol

from app.application.subscription.delete import (
    AsyncUnitOfWork,
    SubscribeDeletedPublisher,
    SubscribeDeletionActor,
    SubscribeDeletionCandidate,
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

    async def delete(self, subscribe_id: int) -> None:
        """把指定订阅登记为待删除。"""
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
    ) -> None:
        """注入数据访问、事务、事件和事件错误处理端口。"""
        self._repository = repository
        self._unit_of_work = unit_of_work
        self._publish_deleted = publish_deleted
        self._handle_event_error = handle_event_error

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
        for candidate in deletions:
            await self._repository.stage_delete(candidate.subscribe_id)

        try:
            await self._unit_of_work.commit()
        except Exception:
            await self._unit_of_work.rollback()
            raise

        for candidate in deletions:
            try:
                await self._publish_deleted(
                    candidate.subscribe_id,
                    dict(candidate.event_payload),
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
