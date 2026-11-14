"""
订阅 Chain 稳定 Facade。
"""

import threading
from typing import Any

from app.application.messaging.subscribe import SubscribeInteractionHandler
from app.application.subscription.execution import SubscriptionExecutionAdmission
from app.chain._interaction import InteractionChainMixin
from app.chain._music import MusicSubscribeMixin
from app.chain.base import ChainBase
from app.chain.download import DownloadChain
from app.chain.media import MediaChain
from app.chain.search.facade import SearchChain
from app.chain.subscribe.completion import SubscribeCompletionOwner
from app.chain.subscribe.create import SubscribeCreateOwner
from app.chain.subscribe.interaction import SubscribeInteractionOwner
from app.chain.subscribe.library import SubscribeLibraryOwner
from app.chain.subscribe.match import SubscribeMatchOwner
from app.chain.subscribe.music import SubscribeMusicProgressOwner
from app.chain.subscribe.notify import SubscribeNotificationOwner
from app.chain.subscribe.policy import SubscribePolicyOwner
from app.chain.subscribe.query import SubscribeQueryOwner
from app.chain.subscribe.reconcile import SubscribeReconciliationOwner
from app.chain.subscribe.refresh import SubscribeRefreshOwner
from app.chain.subscribe.search import SubscribeSearchOwner
from app.domain.context import MediaInfo
from app.runtime.events import Event, eventmanager
from app.schemas.types import EventType


class SubscribeChain(
    MusicSubscribeMixin,
    SubscribeCreateOwner,
    SubscribeSearchOwner,
    SubscribeMatchOwner,
    SubscribeRefreshOwner,
    SubscribeCompletionOwner,
    SubscribeMusicProgressOwner,
    SubscribeQueryOwner,
    SubscribeInteractionOwner,
    SubscribeNotificationOwner,
    SubscribePolicyOwner,
    SubscribeReconciliationOwner,
    SubscribeLibraryOwner,
    InteractionChainMixin,
    ChainBase,
):
    """
    订阅管理稳定 Facade。

    公开入口和类身份保持不变，具体搜索、匹配、刷新、完成、引用协调与通知
    分别由同名包内的单一职责组件 实现。
    """

    _interaction_handler_type = SubscribeInteractionHandler
    _match_lock = threading.Lock()
    _search_queue_lock = threading.Lock()
    _subscription_execution_admission = SubscriptionExecutionAdmission()
    _SUBSCRIPTION_EXECUTION_TTL = 3600 * 2

    @classmethod
    def _music_media_chain(cls) -> MediaChain:
        """
        为音乐订阅组件 提供可替换的媒体识别构造点。
        """
        from app.chain import _music as _music_mixin

        factory = getattr(_music_mixin, "MediaChain", None) or MediaChain
        return factory()

    def _music_download_chain(self) -> DownloadChain:
        """
        为音乐订阅组件 提供可替换的下载构造点。
        """
        from app.chain import _music as _music_mixin

        factory = getattr(_music_mixin, "DownloadChain", None) or DownloadChain
        return factory()

    def _music_search_chain(self) -> SearchChain:
        """
        为音乐订阅组件 提供可替换的搜索构造点。
        """
        from app.chain import _music as _music_mixin

        factory = getattr(_music_mixin, "SearchChain", None) or SearchChain
        return factory()

    def _music_site_keywords(self, mediainfo: MediaInfo) -> list[str]:
        """
        返回音乐订阅目标在各站点使用的检索关键字。
        """
        result: list[str] = SearchChain.music_site_keywords(mediainfo)
        return result

    def _matches_music_resource(self, mediainfo: MediaInfo, *texts: Any) -> bool:
        """
        判断候选文本是否匹配音乐订阅目标。
        """
        result: bool = SearchChain.matches_music_resource(mediainfo, *texts)
        return result

    @eventmanager.register(EventType.SiteDeleted)
    def remove_site(self, event: Event) -> None:
        """
        清理被删除站点在订阅侧的引用。
        """
        return self._remove_site(event)

    @eventmanager.register(EventType.ConfigChanged)
    def reconcile_rule_group_references(self, event: Event) -> None:
        """
        配置变更后协调订阅规则组引用。
        """
        return self._reconcile_rule_group_references(event)

    @eventmanager.register(EventType.TransferComplete)
    def sync_library_progress_after_transfer(self, event: Event) -> None:
        """
        整理入库后同步订阅的入库事实。
        """
        return self._sync_library_progress_after_transfer(event)


# 保持旧插件对类模块身份、repr 与序列化路径的观察结果不变。
SubscribeChain.__module__ = "app.chain.subscribe"
