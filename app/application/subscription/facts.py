"""订阅单轮新鲜媒体事实租约。"""

import copy
from dataclasses import dataclass
from typing import Callable, Optional

from app.application.subscription.contract import SubscriptionSnapshot
from app.domain.context import MediaInfo
from app.schemas.media import resolve_media_identity
from app.schemas.types import MediaType


@dataclass(frozen=True, slots=True)
class FreshFactKey:
    """区分媒体身份、类型、季和剧集组的单轮事实键。"""

    media_source: str
    media_id: str
    media_type: str
    season: Optional[int]
    episode_group: Optional[str]

    @classmethod
    def from_subscribe(cls, subscribe: SubscriptionSnapshot) -> Optional["FreshFactKey"]:
        """从明确媒体身份的订阅构造事实键，身份缺失时禁止跨订阅复用。"""
        media_source, media_id = resolve_media_identity(media=subscribe)
        if not media_source or not media_id:
            return None
        media_type = subscribe.type.value if isinstance(subscribe.type, MediaType) else subscribe.type
        if not media_type:
            return None
        return cls(
            media_source=str(media_source),
            media_id=media_id,
            media_type=media_type,
            season=subscribe.season,
            episode_group=subscribe.episode_group,
        )


class FreshFactLease:
    """在一个批次内合并相同媒体的新鲜识别，并向消费者返回隔离副本。"""

    def __init__(self) -> None:
        """初始化仅在当前调用栈存活的事实缓存和命中计数。"""
        self._facts: dict[FreshFactKey, Optional[MediaInfo]] = {}
        self.loads = 0
        self.hits = 0

    def get_or_load(
        self,
        subscribe: SubscriptionSnapshot,
        loader: Callable[[], Optional[MediaInfo]],
    ) -> Optional[MediaInfo]:
        """读取本轮隔离副本；首次仍由 loader 按 `cache=False` 获取新鲜事实。"""
        key = FreshFactKey.from_subscribe(subscribe)
        if key is None:
            self.loads += 1
            return loader()
        if key in self._facts:
            self.hits += 1
            return copy.deepcopy(self._facts[key])
        self.loads += 1
        fact = loader()
        self._facts[key] = copy.deepcopy(fact)
        return copy.deepcopy(fact)
