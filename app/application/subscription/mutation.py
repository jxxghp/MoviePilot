"""订阅写操作用例及其数据端口。"""

from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol
from uuid import uuid4

from app.application.outbox import AsyncOutboxTransaction, OutboxIntent
from app.schemas.event import SubscribeModifiedEventData


class SubscriptionMutationRepository(Protocol):
    """订阅写用例需要的异步数据端口。"""

    async def async_get(self, subscribe_id: int) -> Any | None:
        """按 ID 获取订阅。"""

    async def async_update(self, subscribe_id: int, payload: dict[str, Any]) -> Any | None:
        """更新订阅。"""

    async def async_stage_update(
        self,
        subscribe_id: int,
        payload: dict[str, Any],
    ) -> Any | None:
        """在调用方事务中暂存更新但不提交。"""

    def get(self, subscribe_id: int) -> Any | None:
        """同步按 ID 获取订阅。"""


class SubscriptionHistoryMutationRepository(Protocol):
    """订阅历史删除用例需要的最小数据端口。"""

    async def async_get(self, history_id: int) -> Any | None:
        """按 ID 获取订阅历史。"""

    async def async_delete(self, history_id: int) -> None:
        """删除订阅历史。"""


class AsyncUnitOfWork(Protocol):
    """订阅修改用例使用的异步事务端口。"""

    async def commit(self) -> None:
        """提交当前订阅修改事务。"""

    async def rollback(self) -> None:
        """回滚当前订阅修改事务。"""


SubscribeModifiedPublisher = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass(frozen=True)
class SubscriptionActor:
    """订阅写操作的权限主体。"""

    name: str
    is_superuser: bool


@dataclass(frozen=True)
class SubscriptionMutation:
    """一次订阅变更前后的稳定快照。"""

    old: dict[str, Any]
    new: dict[str, Any]
    event_published: bool = False


class SubscriptionMutationService:
    """编排订阅访问控制、更新和历史删除。"""

    def __init__(
        self,
        repository: SubscriptionMutationRepository,
        history_repository: SubscriptionHistoryMutationRepository | None = None,
        unit_of_work: AsyncUnitOfWork | None = None,
        outbox: AsyncOutboxTransaction | None = None,
        publish_modified: SubscribeModifiedPublisher | None = None,
    ) -> None:
        """注入订阅数据、事务与 durable 事件端口。"""
        self._repository = repository
        self._history_repository = history_repository
        self._unit_of_work = unit_of_work
        self._outbox = outbox
        self._publish_modified = publish_modified

    async def get_accessible(
        self,
        subscribe_id: int,
        actor: SubscriptionActor,
    ) -> Any | None:
        """读取当前主体可访问的订阅。"""
        subscribe = await self._repository.async_get(subscribe_id)
        return subscribe if self.can_access(subscribe, actor) else None

    def get_accessible_sync(
        self,
        subscribe_id: int,
        actor: SubscriptionActor,
    ) -> Any | None:
        """同步读取当前主体可访问的订阅。"""
        subscribe = self._repository.get(subscribe_id)
        return subscribe if self.can_access(subscribe, actor) else None

    async def update(
        self,
        subscribe_id: int,
        payload: dict[str, Any],
        actor: SubscriptionActor,
        existing: Any | None = None,
        scene: str = "update",
    ) -> SubscriptionMutation | None:
        """更新订阅，并在同一事务暂存可恢复的 SubscribeModified 事件。"""
        subscribe = existing or await self.get_accessible(subscribe_id, actor)
        if subscribe and not self.can_access(subscribe, actor):
            return None
        if not subscribe:
            return None
        old = subscribe.to_dict()
        if not self._unit_of_work:
            updated = await self._repository.async_update(subscribe_id, payload)
            return SubscriptionMutation(old=old, new=updated.to_dict() if updated else {})

        if not self._outbox or not self._publish_modified:
            raise RuntimeError("订阅修改事务缺少 outbox 或事件发布端口")
        try:
            updated = await self._repository.async_stage_update(subscribe_id, payload)
            if not updated:
                return None
            event_payload = SubscribeModifiedEventData(
                subscribe_id=subscribe_id,
                old_subscribe_info=old,
                subscribe_info=updated.to_dict(),
                scene=scene,
            ).to_dict()
            event_key = _modified_event_key(subscribe_id, scene)
            event_payload["idempotency_key"] = event_key
            await self._outbox.stage(
                OutboxIntent(
                    event_key=event_key,
                    topic="subscribe.modified",
                    payload=event_payload,
                ),
                datetime.now(timezone.utc),
            )
            await self._unit_of_work.commit()
        except Exception:
            await self._unit_of_work.rollback()
            raise

        await self._publish_modified(event_payload)
        await self._outbox.complete_by_event_key(
            event_key,
            datetime.now(timezone.utc),
        )
        return SubscriptionMutation(
            old=old,
            new=event_payload["subscribe_info"],
            event_published=True,
        )

    async def update_status(
        self,
        subscribe_id: int,
        state: str,
        actor: SubscriptionActor,
    ) -> SubscriptionMutation | None:
        """更新订阅状态并返回前后快照。"""
        return await self.update(
            subscribe_id,
            {"state": state},
            actor,
            scene="status",
        )

    async def reset(
        self,
        subscribe_id: int,
        actor: SubscriptionActor,
    ) -> SubscriptionMutation | None:
        """重置订阅进度和手工集数标记。"""
        subscribe = await self.get_accessible(subscribe_id, actor)
        if not subscribe:
            return None
        payload = {
            "note": [],
            "lack_episode": subscribe.total_episode,
            "current_priority": None,
            "current_audio_format": None,
            "current_bitrate": None,
            "current_bit_depth": None,
            "current_sample_rate": None,
            "episode_priority": {},
            "manual_total_episode": 0,
            "state": "R",
        }
        return await self.update(
            subscribe_id,
            payload,
            actor,
            existing=subscribe,
            scene="reset",
        )

    async def delete_history(
        self,
        history_id: int,
        actor: SubscriptionActor,
    ) -> bool:
        """删除当前主体可访问的订阅历史。"""
        if self._history_repository is None:
            raise RuntimeError("订阅历史数据端口未配置")
        history = await self._history_repository.async_get(history_id)
        if not self.can_access(history, actor):
            return False
        await self._history_repository.async_delete(history_id)
        return True

    @staticmethod
    def can_access(subscribe: Any, actor: SubscriptionActor) -> bool:
        """判断主体是否可访问订阅或订阅历史。"""
        if not subscribe:
            return False
        if actor.is_superuser:
            return True
        username = getattr(subscribe, "username", None)
        return bool(username) and username == actor.name


def _modified_event_key(subscribe_id: int, scene: str) -> str:
    """为一次订阅修改生成重试期间稳定且跨多次相同变更不碰撞的幂等键。"""
    return f"subscribe.modified:{subscribe_id}:{scene}:{uuid4().hex}:v1"


SubscriptionMutationScope = Callable[
    [],
    AbstractAsyncContextManager[SubscriptionMutationService],
]
_configured_mutation_scope: SubscriptionMutationScope | None = None


def configure_subscription_mutation_scope(
    provider: SubscriptionMutationScope,
) -> None:
    """由启动组合根登记 Agent 等非 HTTP 入口使用的事务作用域。"""
    global _configured_mutation_scope
    _configured_mutation_scope = provider


def get_subscription_mutation_scope() -> AbstractAsyncContextManager[SubscriptionMutationService]:
    """返回一次独占会话的订阅修改服务作用域。"""
    if _configured_mutation_scope is None:
        raise RuntimeError("订阅修改事务作用域尚未配置")
    return _configured_mutation_scope()
