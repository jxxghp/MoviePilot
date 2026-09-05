"""手工订阅搜索应用用例。"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Optional

from app.application.subscription.contract import (
    SubscribeDeletionCandidate,
    SubscriptionStagingPort,
)


@dataclass(frozen=True, slots=True)
class SubscribeSearchActor:
    """执行手工订阅搜索的用户身份。"""

    username: str
    is_superuser: bool


@dataclass(frozen=True, slots=True)
class SubscriptionSearchSubmission:
    """一次手工搜索请求的入队结果。"""

    batch_ids: tuple[str, ...]
    target_count: int
    queued_count: int
    ongoing_count: int
    single: bool

    @property
    def batch_id(self) -> Optional[str]:
        """返回最适合前端立即跟踪的批次编号。"""
        return self.batch_ids[0] if self.batch_ids else None


SubscribeSearchSubmitter = Callable[
    [tuple[int, ...], bool],
    Awaitable[SubscriptionSearchSubmission],
]


class SearchSubscriptionsCommand:
    """按用户权限读取目标，并把手工搜索轻量提交到持久队列。"""

    def __init__(
        self,
        repository: SubscriptionStagingPort,
        submit_search: SubscribeSearchSubmitter,
    ) -> None:
        """注入订阅读取端口和持久队列提交端口。"""
        self._repository = repository
        self._submit_search = submit_search

    async def execute(
        self,
        actor: SubscribeSearchActor,
        subscribe_id: int | None = None,
    ) -> Optional[SubscriptionSearchSubmission]:
        """提交单条或当前用户全部可搜索订阅，目标不可访问时返回空。"""
        if subscribe_id is not None:
            candidate = await self._repository.get_candidate(subscribe_id)
            if not self._can_access(candidate, actor):
                return None
            return await self._submit_search((subscribe_id,), True)

        subscribe_ids = await self._repository.list_search_ids(
            None if actor.is_superuser else actor.username,
            "R",
        )
        if not subscribe_ids:
            return SubscriptionSearchSubmission(
                batch_ids=(),
                target_count=0,
                queued_count=0,
                ongoing_count=0,
                single=False,
            )
        return await self._submit_search(tuple(subscribe_ids), False)

    @staticmethod
    def _can_access(
        candidate: SubscribeDeletionCandidate | None,
        actor: SubscribeSearchActor,
    ) -> bool:
        """沿用订阅读取接口的超级用户和归属用户权限语义。"""
        if candidate is None:
            return False
        if actor.is_superuser:
            return True
        return bool(candidate.username) and candidate.username == actor.username
