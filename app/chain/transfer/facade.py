"""
整理链稳定门面；实现职责由同名包内的职责组件 承担。
"""

from app.chain.base import ChainBase
from app.chain.media import MediaChain
from app.chain.storage import StorageChain
from app.foundation.singleton import Singleton
from app.runtime.reload import ConfigReloadMixin

from .execution import TransferExecutionOwner
from .filter import FileFilterMixin
from .format import EpisodeFormatMixin
from .history import TransferHistoryOwner
from .plan import TransferPlanningOwner
from .queue import TransferQueueOwner
from .records import FileKeyMixin, HistoryMatchMixin, ManualHistoryMixin
from .retry import FailedRetryMixin
from .scrape import ScrapeBatchMixin
from .settlement import TransferSettlementOwner
from .workflow import TransferWorkflowOwner


class TransferChain(
        FileFilterMixin,
        ScrapeBatchMixin,
        EpisodeFormatMixin,
        HistoryMatchMixin,
        FileKeyMixin,
        ManualHistoryMixin,
        FailedRetryMixin,
        TransferQueueOwner,
        TransferPlanningOwner,
        TransferExecutionOwner,
        TransferSettlementOwner,
        TransferWorkflowOwner,
        TransferHistoryOwner,
        ChainBase,
        ConfigReloadMixin,
        metaclass=Singleton,
):
    """
    保留文件整理处理链的稳定类型身份并组合单一职责组件。
    """

    @classmethod
    def _transfer_media_chain(cls) -> MediaChain:
        """
        为整理 mixin 提供可替换的媒体识别构造点。
        """
        from . import filter as transfer_filter
        factory = getattr(transfer_filter, "MediaChain", None) or MediaChain
        return factory()

    @classmethod
    def _transfer_storage_chain(cls) -> StorageChain:
        """
        为整理 mixin 提供可替换的存储构造点。
        """
        from . import records as transfer_records
        factory = getattr(transfer_records, "StorageChain", None) or StorageChain
        return factory()

    @classmethod
    def _transfer_subscribe_chain(cls) -> ChainBase:
        """
        为整理 mixin 提供可替换的订阅构造点。
        """
        from app.chain.subscribe.facade import SubscribeChain as _SubscribeChain
        return _SubscribeChain()

    _retain_failed_singleton = True

    CONFIG_WATCH = {
        "TRANSFER_THREADS",
        "RMT_MEDIAEXT",
        "RMT_SUBEXT",
        "RMT_AUDIOEXT",
    }

    _WORKER_RESTART_TIMEOUT_SECONDS = 30.0

    _WORKER_CLOSE_TIMEOUT_SECONDS = 30.0

    _QUEUE_STOP_SENTINEL = object()

    _WORKER_LEASE_SECONDS = 120

    _LEASE_HEARTBEAT_INTERVAL_SECONDS = 30.0

    _RECOVERY_POLL_INTERVAL_SECONDS = 15.0

    _RECOVERY_CLAIM_LIMIT = 100


# 保持插件对类模块身份、repr 与 pickle 路径的观察结果不变。
TransferChain.__module__ = "app.chain.transfer"
