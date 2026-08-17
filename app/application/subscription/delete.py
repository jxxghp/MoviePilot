"""订阅删除应用用例及其依赖端口。"""

from dataclasses import dataclass
from typing import Awaitable, Callable, Mapping, Protocol


@dataclass(frozen=True)
class SubscribeDeletionActor:
    """执行订阅删除的用户身份。"""

    username: str
    is_superuser: bool


@dataclass(frozen=True)
class SubscribeDeletionCandidate:
    """删除前读取出的订阅快照，不向应用层暴露 ORM 对象。"""

    subscribe_id: int
    username: str | None
    event_payload: Mapping[str, object]


class SubscribeDeletionRepository(Protocol):
    """订阅删除用例需要的最小数据访问端口。"""

    async def get_candidate(
        self,
        subscribe_id: int,
    ) -> SubscribeDeletionCandidate | None:
        """读取订阅及删除事件所需的稳定快照。"""
        ...

    async def stage_delete(self, subscribe_id: int) -> None:
        """把已读取的订阅登记为待删除，但不自行提交事务。"""
        ...


class AsyncUnitOfWork(Protocol):
    """订阅写用例使用的异步事务端口。"""

    async def commit(self) -> None:
        """提交当前事务。"""
        ...

    async def rollback(self) -> None:
        """回滚当前事务。"""
        ...


SubscribeDeletedPublisher = Callable[
    [int, Mapping[str, object]],
    Awaitable[None],
]
SubscribeDeletedReporter = Callable[[Mapping[str, object]], object]


class DeleteSubscribeCommand:
    """按权限删除订阅，并在提交成功后依次发送事件和统计上报。"""

    def __init__(
        self,
        repository: SubscribeDeletionRepository,
        unit_of_work: AsyncUnitOfWork,
        publish_deleted: SubscribeDeletedPublisher,
        report_deleted: SubscribeDeletedReporter,
    ) -> None:
        """注入数据访问、事务与提交后副作用端口。"""
        self._repository = repository
        self._unit_of_work = unit_of_work
        self._publish_deleted = publish_deleted
        self._report_deleted = report_deleted

    async def execute(
        self,
        subscribe_id: int,
        actor: SubscribeDeletionActor,
    ) -> bool:
        """
        删除当前用户可访问的订阅。

        返回 False 表示订阅不存在或无权访问；该结果由 API 映射为历史兼容的成功响应。
        提交后的事件与上报保持原有顺序，任一副作用失败都会继续向调用方抛出。
        """
        candidate = await self._repository.get_candidate(subscribe_id)
        if not self._can_delete(candidate, actor):
            return False

        await self._repository.stage_delete(subscribe_id)
        try:
            await self._unit_of_work.commit()
        except Exception:
            await self._unit_of_work.rollback()
            raise

        event_payload = dict(candidate.event_payload)
        await self._publish_deleted(subscribe_id, event_payload)
        self._report_deleted(
            {
                "media_source": event_payload.get("media_source"),
                "media_id": event_payload.get("media_id"),
                "season": event_payload.get("season"),
            }
        )
        return True

    @staticmethod
    def _can_delete(
        candidate: SubscribeDeletionCandidate | None,
        actor: SubscribeDeletionActor,
    ) -> bool:
        """判断用户是否拥有目标订阅的删除权限。"""
        if candidate is None:
            return False
        if actor.is_superuser:
            return True
        return bool(candidate.username) and candidate.username == actor.username
