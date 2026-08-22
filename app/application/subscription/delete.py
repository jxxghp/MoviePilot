"""订阅删除应用用例及其依赖端口。"""

from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Mapping, Optional, Protocol, cast
from uuid import uuid4

from app.application.outbox import AsyncOutboxTransaction, OutboxIntent
from app.schemas.event import SubscribeDeletedEventData
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


SubscribeDeletedPublisher = Callable[[dict[str, Any]], Awaitable[None]]
SubscribeDeletedReporter = Callable[[Mapping[str, object]], object]


class DeleteSubscribeCommand:
    """按权限删除订阅，并在提交成功后依次发送事件和统计上报。"""

    def __init__(
        self,
        repository: SubscribeDeletionRepository,
        unit_of_work: AsyncUnitOfWork,
        publish_deleted: SubscribeDeletedPublisher,
        report_deleted: SubscribeDeletedReporter,
        outbox: AsyncOutboxTransaction | None = None,
    ) -> None:
        """注入数据访问、事务与提交后副作用端口。"""
        self._repository = repository
        self._unit_of_work = unit_of_work
        self._publish_deleted = publish_deleted
        self._report_deleted = report_deleted
        self._outbox = outbox

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
        assert candidate is not None

        await self._repository.stage_delete(subscribe_id)
        event_payload = build_subscribe_deleted_payload(
            subscribe_id,
            candidate.event_payload,
        )
        event_key = event_payload["idempotency_key"]
        try:
            if self._outbox:
                await self._outbox.stage(
                    OutboxIntent(
                        event_key=event_key,
                        topic="subscribe.deleted",
                        payload=event_payload,
                    ),
                    datetime.now(timezone.utc),
                )
            await self._unit_of_work.commit()
        except Exception:
            await self._unit_of_work.rollback()
            raise

        await self._publish_deleted(event_payload)
        if self._outbox:
            await self._outbox.complete_by_event_key(
                event_key,
                datetime.now(timezone.utc),
            )
        # 上报适配器会自行白名单过滤公开字段；传完整删除前快照可保留音乐实体维度，
        # 避免 Agent 与 API 入口收敛后丢失 music_type / total_tracks。
        self._report_deleted(dict(candidate.event_payload))
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


def build_subscribe_deleted_payload(
    subscribe_id: int,
    subscribe_info: Mapping[str, object],
) -> dict[str, Any]:
    """构造兼容旧字段并携带幂等键的订阅删除事件快照。"""
    event_key = f"subscribe.deleted:{subscribe_id}:{uuid4().hex}:v1"
    return cast(
        dict[str, Any],
        SubscribeDeletedEventData(
            subscribe_id=subscribe_id,
            subscribe_info=dict(subscribe_info),
            idempotency_key=event_key,
        ).model_dump(mode="json"),
    )


DeleteSubscribeScope = Callable[[], AbstractAsyncContextManager[DeleteSubscribeCommand]]
_configured_delete_scope: DeleteSubscribeScope | None = None


def configure_delete_subscribe_scope(provider: DeleteSubscribeScope) -> None:
    """由启动组合根登记非 HTTP 入口使用的订阅删除事务作用域。"""
    global _configured_delete_scope
    _configured_delete_scope = provider


def get_delete_subscribe_scope() -> AbstractAsyncContextManager[DeleteSubscribeCommand]:
    """返回一次独占会话的订阅删除命令作用域。"""
    if _configured_delete_scope is None:
        raise RuntimeError("订阅删除事务作用域尚未配置")
    return _configured_delete_scope()


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
