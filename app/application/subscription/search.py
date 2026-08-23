"""手工订阅搜索应用用例。"""

from dataclasses import dataclass
from typing import Callable, Protocol

from app.application.subscription.delete import SubscribeDeletionCandidate


@dataclass(frozen=True, slots=True)
class SubscribeSearchActor:
    """执行手工订阅搜索的用户身份。"""

    username: str
    is_superuser: bool


class SubscribeSearchRepository(Protocol):
    """手工订阅搜索所需的最小读取端口。"""

    async def get_candidate(
            self,
            subscribe_id: int,
    ) -> SubscribeDeletionCandidate | None:
        """读取单条订阅的归属信息。"""
        ...

    async def list_search_ids(self, username: str, state: str) -> list[int]:
        """返回用户当前可搜索状态下的订阅编号。"""
        ...


SubscribeSearchScheduler = Callable[[tuple[int, ...] | None, str | None], None]


class SearchSubscriptionsCommand:
    """按用户权限生成并提交手工订阅搜索任务。"""

    def __init__(
            self,
            repository: SubscribeSearchRepository,
            schedule_search: SubscribeSearchScheduler,
    ) -> None:
        """注入订阅读取端口和后台任务提交端口。"""
        self._repository = repository
        self._schedule_search = schedule_search

    async def execute(
            self,
            actor: SubscribeSearchActor,
            subscribe_id: int | None = None,
    ) -> bool:
        """提交单条或当前用户全部可搜索订阅，返回目标是否存在。"""
        if subscribe_id is not None:
            candidate = await self._repository.get_candidate(subscribe_id)
            if not self._can_access(candidate, actor):
                return False
            self._schedule_search((subscribe_id,), None)
            return True

        if actor.is_superuser:
            self._schedule_search(None, "R")
            return True

        subscribe_ids = await self._repository.list_search_ids(
            actor.username,
            "R",
        )
        if subscribe_ids:
            self._schedule_search(tuple(subscribe_ids), None)
        return True

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
