"""订阅写操作用例及其数据端口。"""

from dataclasses import dataclass
from typing import Any, Protocol


class SubscriptionMutationRepository(Protocol):
    """订阅写用例需要的异步数据端口。"""

    async def async_get(self, subscribe_id: int) -> Any | None:
        """按 ID 获取订阅。"""

    async def async_update(self, subscribe_id: int, payload: dict[str, Any]) -> Any | None:
        """更新订阅。"""

    def get(self, subscribe_id: int) -> Any | None:
        """同步按 ID 获取订阅。"""


class SubscriptionHistoryMutationRepository(Protocol):
    """订阅历史删除用例需要的最小数据端口。"""

    async def async_get(self, history_id: int) -> Any | None:
        """按 ID 获取订阅历史。"""

    async def async_delete(self, history_id: int) -> None:
        """删除订阅历史。"""


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


class SubscriptionMutationService:
    """编排订阅访问控制、更新和历史删除。"""

    def __init__(
        self,
        repository: SubscriptionMutationRepository,
        history_repository: SubscriptionHistoryMutationRepository | None = None,
    ) -> None:
        """注入订阅和订阅历史数据端口。"""
        self._repository = repository
        self._history_repository = history_repository

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
    ) -> SubscriptionMutation | None:
        """更新当前主体可访问的订阅并返回前后快照。"""
        subscribe = existing or await self.get_accessible(subscribe_id, actor)
        if subscribe and not self.can_access(subscribe, actor):
            return None
        if not subscribe:
            return None
        old = subscribe.to_dict()
        updated = await self._repository.async_update(subscribe_id, payload)
        return SubscriptionMutation(old=old, new=updated.to_dict() if updated else {})

    async def update_status(
        self,
        subscribe_id: int,
        state: str,
        actor: SubscriptionActor,
    ) -> SubscriptionMutation | None:
        """更新订阅状态并返回前后快照。"""
        return await self.update(subscribe_id, {"state": state}, actor)

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
        old = subscribe.to_dict()
        updated = await self._repository.async_update(subscribe_id, payload)
        return SubscriptionMutation(old=old, new=updated.to_dict() if updated else {})

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
