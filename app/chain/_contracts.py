"""Chain mixin 对宿主能力的静态契约。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from app.application.classification.execution import ClassificationExecutionPort
    from app.application.history import TransferHistoryRepository
    from app.application.transfer.execution import TransferExecutionRepository


class ChainRuntimeMixinHost(Protocol):
    """识别与消息 mixin 共用的 Chain 运行能力。"""

    runtime_config: Any
    eventmanager: Any
    messageoper: Any
    messagequeue: Any
    classification_service: ClassificationExecutionPort | None

    def run_module(self, method: str, **kwargs: Any) -> Any:
        """调用同步模块能力。"""
        ...

    def run_module_strict(self, method: str, **kwargs: Any) -> Any:
        """同步调用模块能力，并向调用方传播 provider 失败。"""
        ...

    async def async_run_module(self, method: str, **kwargs: Any) -> Any:
        """调用异步模块能力。"""
        ...


class MusicSubscribeMixinHost(Protocol):
    """音乐订阅 mixin 对 SubscribeChain 的最小要求。"""

    @classmethod
    def _music_media_chain(cls) -> Any:
        """构造媒体识别链。"""
        ...

    def _music_download_chain(self) -> Any:
        """构造下载链。"""
        ...

    def _music_search_chain(self) -> Any:
        """构造搜索链。"""
        ...

    def _music_site_keywords(self, mediainfo: Any) -> list[str]:
        """构造音乐站点搜索关键字。"""
        ...

    def _matches_music_resource(self, mediainfo: Any, *texts: Any) -> bool:
        """判断站点资源文本是否匹配音乐目标。"""
        ...

    def get_sub_sites(self, subscribe: Any) -> list[int]: ...

    def get_params(self, subscribe: Any) -> Any: ...

    def filter_torrents(self, *args: Any, **kwargs: Any) -> Any: ...

    def check_and_handle_existing_media(self, *args: Any, **kwargs: Any) -> Any: ...

    def finish_subscribe_or_not(self, *args: Any, **kwargs: Any) -> Any: ...

    def get_subscribe_source_keyword(self, subscribe: Any) -> str: ...

    def _SubscribeChain__candidate_contract_changed(
        self,
        prepared: Any,
        current: Any,
    ) -> bool:
        """判断准备候选期间订阅身份或过滤合同是否已经变化。"""
        ...


class InteractionMixinHost(Protocol):
    """交互委托 mixin 对业务 Chain 的最小要求。"""

    _interaction_handler_type: type

    def _interaction_handler(self) -> Any:
        """构造业务交互处理器。"""
        ...


class TransferMixinHost(ChainRuntimeMixinHost, Protocol):
    """整理辅助 mixin 对 TransferChain 的最小要求。"""

    @classmethod
    def _transfer_media_chain(cls) -> Any:
        """构造媒体识别链。"""
        ...

    @classmethod
    def _transfer_storage_chain(cls) -> Any:
        """构造存储链。"""
        ...

    @classmethod
    def _transfer_subscribe_chain(cls) -> Any:
        """构造订阅链。"""
        ...

    def post_message(self, *args: Any, **kwargs: Any) -> Any: ...

    async def async_post_message(self, *args: Any, **kwargs: Any) -> Any: ...

    def obtain_images(self, *args: Any, **kwargs: Any) -> Any: ...

    def do_transfer(self, *args: Any, **kwargs: Any) -> Any: ...
    transfer_history_repository: TransferHistoryRepository
    transfer_execution_repository: TransferExecutionRepository
