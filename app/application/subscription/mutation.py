"""订阅写操作用例及其数据端口。"""

from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager, AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, cast
from uuid import uuid4

from app.application.classification.reference import (
    normalize_classification_reference_payload,
)
from app.application.outbox import (
    SUBSCRIBE_MODIFIED_TOPIC,
    AsyncOutboxDispatchStore,
    AsyncOutboxStager,
    OutboxDispatchStore,
    OutboxIntent,
    OutboxStager,
    SyncUnitOfWork,
    deliver_async_outbox_effect,
    deliver_outbox_effect,
)
from app.application.subscription.contract import (
    SessionSubscriptionPort,
    SubscriptionHistorySnapshot,
    SubscriptionHistoryStagingPort,
    SubscriptionPatch,
    SubscriptionSnapshot,
)
from app.application.subscription.delete import AsyncUnitOfWork
from app.schemas.common import JsonData
from app.schemas.event import SubscribeModifiedEventData

SubscribeModifiedPublisher = Callable[[dict[str, JsonData]], Awaitable[None]]
SyncSubscribeModifiedPublisher = Callable[[dict[str, JsonData]], None]


def _normalized_subscription_patch(
    subscribe: SubscriptionSnapshot,
    payload: dict[str, JsonData],
) -> SubscriptionPatch:
    """按订阅媒体类型规范化人工分类稳定引用并构造写入补丁。"""
    normalized = normalize_classification_reference_payload(
        payload,
        media_type=subscribe.type,
    )
    return SubscriptionPatch(cast(dict[str, JsonData], normalized))


@dataclass(frozen=True, slots=True)
class SubscriptionActor:
    """订阅写操作的权限主体。"""

    name: str
    is_superuser: bool


SubscribeHistoryDeletionStatus = Literal["deleted", "not_found", "forbidden"]


@dataclass(frozen=True, slots=True)
class SubscriptionMutation:
    """一次订阅变更前后的稳定快照。"""

    snapshot: SubscriptionSnapshot
    old: dict[str, JsonData]
    new: dict[str, JsonData]
    event_published: bool = False
    business_committed: bool = False
    pending_effects: tuple[str, ...] = ()


def _modified_event(
    subscribe_id: int,
    scene: str,
    old: dict[str, JsonData],
    updated: SubscriptionSnapshot,
) -> tuple[str, dict[str, JsonData]]:
    """构造订阅修改 intent 共用的稳定键和事件快照。"""
    event_payload = SubscribeModifiedEventData(
        subscribe_id=subscribe_id,
        old_subscribe_info=old,
        subscribe_info=updated.to_dict(),
        scene=scene,
    ).to_dict()
    event_key = _modified_event_key(subscribe_id, scene)
    event_payload["idempotency_key"] = event_key
    return event_key, event_payload


def _reset_payload(subscribe: SubscriptionSnapshot) -> dict[str, JsonData]:
    """构造同步与异步重置共享的订阅字段补丁。"""
    return {
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


class SyncSubscriptionMutationService:
    """用一个同步 UoW 原子提交订阅修改和 durable 事件 intent。"""

    def __init__(
        self,
        repository: SessionSubscriptionPort,
        unit_of_work: SyncUnitOfWork,
        outbox: OutboxStager,
        dispatch_store: OutboxDispatchStore,
        publish_modified: SyncSubscribeModifiedPublisher,
    ) -> None:
        """注入同一 Session 的仓储、事务、outbox 与提交后发布端口。"""
        self._repository = repository
        self._unit_of_work = unit_of_work
        self._outbox = outbox
        self._dispatch_store = dispatch_store
        self._publish_modified = publish_modified

    def get_accessible(
        self,
        subscribe_id: int,
        actor: SubscriptionActor,
    ) -> SubscriptionSnapshot | None:
        """同步读取当前主体可访问的订阅。"""
        subscribe = self._repository.get(subscribe_id)
        return subscribe if SubscriptionMutationService.can_access(subscribe, actor) else None

    def update(
        self,
        subscribe_id: int,
        payload: dict[str, JsonData],
        actor: SubscriptionActor,
        existing: SubscriptionSnapshot | None = None,
        scene: str = "update",
    ) -> SubscriptionMutation | None:
        """同步更新订阅，并在同一事务暂存可恢复的修改事件。"""
        subscribe = existing or self.get_accessible(subscribe_id, actor)
        if subscribe and not SubscriptionMutationService.can_access(subscribe, actor):
            return None
        if not subscribe:
            return None
        old = subscribe.to_dict()
        try:
            updated = self._repository.stage_update(
                subscribe_id,
                _normalized_subscription_patch(subscribe, payload),
            )
            if not updated:
                self._unit_of_work.rollback()
                return None
            event_key, event_payload = _modified_event(
                subscribe_id,
                scene,
                old,
                updated,
            )
            self._outbox.stage(
                OutboxIntent(
                    event_key=event_key,
                    topic=SUBSCRIBE_MODIFIED_TOPIC,
                    payload=event_payload,
                ),
                datetime.now(timezone.utc),
            )
            self._unit_of_work.commit()
        except Exception:
            self._unit_of_work.rollback()
            raise

        delivered = deliver_outbox_effect(
            self._dispatch_store,
            event_key,
            lambda: self._publish_modified(event_payload),
        )
        return SubscriptionMutation(
            snapshot=updated,
            old=old,
            new=updated.to_dict(),
            event_published=delivered,
            business_committed=True,
            pending_effects=() if delivered else (event_key,),
        )

    def update_status(
        self,
        subscribe_id: int,
        state: str,
        actor: SubscriptionActor,
    ) -> SubscriptionMutation | None:
        """同步更新订阅状态并返回前后快照。"""
        return self.update(
            subscribe_id,
            {"state": state},
            actor,
            scene="status",
        )

    def reset(
        self,
        subscribe_id: int,
        actor: SubscriptionActor,
    ) -> SubscriptionMutation | None:
        """同步重置订阅进度和手工集数标记。"""
        subscribe = self.get_accessible(subscribe_id, actor)
        if not subscribe:
            return None
        return self.update(
            subscribe_id,
            _reset_payload(subscribe),
            actor,
            existing=subscribe,
            scene="reset",
        )


class SubscriptionMutationService:
    """编排订阅访问控制、更新和历史删除。"""

    def __init__(
        self,
        repository: SessionSubscriptionPort,
        unit_of_work: AsyncUnitOfWork,
        outbox: AsyncOutboxStager,
        dispatch_store: AsyncOutboxDispatchStore,
        publish_modified: SubscribeModifiedPublisher,
        history_repository: SubscriptionHistoryStagingPort | None = None,
    ) -> None:
        """注入订阅数据、事务与 durable 事件端口。"""
        self._repository = repository
        self._history_repository = history_repository
        self._unit_of_work = unit_of_work
        self._outbox = outbox
        self._dispatch_store = dispatch_store
        self._publish_modified = publish_modified

    async def get_accessible(
        self,
        subscribe_id: int,
        actor: SubscriptionActor,
    ) -> SubscriptionSnapshot | None:
        """读取当前主体可访问的订阅。"""
        subscribe = await self._repository.async_get(subscribe_id)
        return subscribe if self.can_access(subscribe, actor) else None

    async def update(
        self,
        subscribe_id: int,
        payload: dict[str, JsonData],
        actor: SubscriptionActor,
        existing: SubscriptionSnapshot | None = None,
        scene: str = "update",
    ) -> SubscriptionMutation | None:
        """更新订阅，并在同一事务暂存可恢复的 SubscribeModified 事件。"""
        subscribe = existing or await self.get_accessible(subscribe_id, actor)
        if subscribe and not self.can_access(subscribe, actor):
            return None
        if not subscribe:
            return None
        old = subscribe.to_dict()
        try:
            updated = await self._repository.async_stage_update(
                subscribe_id,
                _normalized_subscription_patch(subscribe, payload),
            )
            if not updated:
                await self._unit_of_work.rollback()
                return None
            event_key, event_payload = _modified_event(
                subscribe_id,
                scene,
                old,
                updated,
            )
            await self._outbox.stage(
                OutboxIntent(
                    event_key=event_key,
                    topic=SUBSCRIBE_MODIFIED_TOPIC,
                    payload=event_payload,
                ),
                datetime.now(timezone.utc),
            )
            await self._unit_of_work.commit()
        except Exception:
            await self._unit_of_work.rollback()
            raise

        async def publish_event() -> None:
            """发布本次事务已持久化的订阅修改事件。"""
            await self._publish_modified(event_payload)

        delivered = await deliver_async_outbox_effect(
            self._dispatch_store,
            event_key,
            publish_event,
        )
        return SubscriptionMutation(
            snapshot=updated,
            old=old,
            new=updated.to_dict(),
            event_published=delivered,
            business_committed=True,
            pending_effects=() if delivered else (event_key,),
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
        return await self.update(
            subscribe_id,
            _reset_payload(subscribe),
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
        return (await self.delete_history_with_status(history_id, actor)) == "deleted"

    async def delete_history_with_status(
        self,
        history_id: int,
        actor: SubscriptionActor,
    ) -> SubscribeHistoryDeletionStatus:
        """删除订阅历史并返回可供 HTTP 层判断的结果状态。"""
        if self._history_repository is None:
            raise RuntimeError("订阅历史数据端口未配置")
        history = await self._history_repository.async_get(history_id)
        if history is None:
            return "not_found"
        if not self.can_access(history, actor):
            return "forbidden"
        try:
            await self._history_repository.stage_delete(history_id)
            await self._unit_of_work.commit()
        except Exception:
            await self._unit_of_work.rollback()
            raise
        return "deleted"

    @staticmethod
    def can_access(
        subscribe: SubscriptionSnapshot | SubscriptionHistorySnapshot | None,
        actor: SubscriptionActor,
    ) -> bool:
        """判断主体是否可访问订阅或订阅历史。"""
        if not subscribe:
            return False
        if actor.is_superuser:
            return True
        username = subscribe.username
        return bool(username) and username == actor.name


def _modified_event_key(subscribe_id: int, scene: str) -> str:
    """为一次订阅修改生成重试期间稳定且跨多次相同变更不碰撞的幂等键。"""
    return f"subscribe.modified:{subscribe_id}:{scene}:{uuid4().hex}:v1"


SubscriptionMutationScope = Callable[
    [],
    AbstractAsyncContextManager[SubscriptionMutationService],
]
SyncSubscriptionMutationScope = Callable[
    [],
    AbstractContextManager[SyncSubscriptionMutationService],
]
