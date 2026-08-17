"""订阅删除应用用例及其依赖端口。"""

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping, Optional, Protocol

from app.schemas.types import MediaSource


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


class SubscribeDeletionCandidateRepository:
    """把持久化身份仓库适配为返回 SubscribeDeletionCandidate 的仓库端口。

    订阅删除/搜索三个用例（本模块的 DeleteSubscribeCommand，以及
    app/application/subscription/identity.py、search.py 的两个用例）都要求
    repository.get_candidate / list_candidates_by_identity 直接返回
    SubscribeDeletionCandidate；但持久化仓库（如 SubscribeOper）只收发
    subscribe_id/username/event_payload 这类持久化字段，不认识应用层类型。
    本类在组合根与持久化仓库之间做这层翻译，使持久化仓库不必反向依赖应用层。
    """

    def __init__(self, repository: Any) -> None:
        """
        :param repository: 提供持久化身份字段的仓库，须实现 get_candidate 与
            list_candidates_by_identity，返回含 subscribe_id/username/event_payload
            三个键的字典
        """
        self._repository = repository

    async def get_candidate(
        self,
        subscribe_id: int,
    ) -> Optional[SubscribeDeletionCandidate]:
        """
        读取单条订阅的持久化身份快照，翻译为订阅删除快照。

        :param subscribe_id: 订阅 ID
        :return: 订阅删除快照；订阅不存在时为 None
        """
        row = await self._repository.get_candidate(subscribe_id)
        return SubscribeDeletionCandidate(**row) if row else None

    async def list_candidates_by_identity(
        self,
        media_source: MediaSource,
        media_id: str,
        season: Optional[int],
        music_type: Optional[str],
    ) -> list[SubscribeDeletionCandidate]:
        """
        按媒体身份读取去重后的持久化身份快照，翻译为订阅删除快照列表。

        :param media_source: 媒体来源
        :param media_id: 媒体 ID
        :param season: 季号，为 None 时不按季过滤
        :param music_type: 音乐子类型，为 None 时不按子类型过滤
        :return: 订阅删除快照列表
        """
        rows = await self._repository.list_candidates_by_identity(
            media_source, media_id, season, music_type,
        )
        return [SubscribeDeletionCandidate(**row) for row in rows]

    def __getattr__(self, name: str) -> Any:
        """
        代理未重写的方法与属性给底层持久化仓库（如 stage_delete、list_search_ids）。

        :param name: 属性或方法名
        :return: 底层仓库上的同名属性
        """
        return getattr(self._repository, name)
