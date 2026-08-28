"""整理链稳定 Facade；实现职责由同名包内 owner 承担。"""

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
    """保留文件整理处理链的稳定类型身份并组合单一职责 owner。"""

    @classmethod
    def _transfer_media_chain(cls):
        """为整理 mixin 提供可替换的媒体识别构造点。"""
        from . import filter as transfer_filter
        return (transfer_filter.MediaChain or MediaChain)()

    @classmethod
    def _transfer_storage_chain(cls) -> StorageChain:
        """为整理 mixin 提供可替换的存储构造点。"""
        from . import records as transfer_records
        return (transfer_records.StorageChain or StorageChain)()

    @classmethod
    def _transfer_subscribe_chain(cls):
        """为整理 mixin 提供可替换的订阅构造点。"""
        from app.chain.subscribe.facade import SubscribeChain as _SubscribeChain
        return _SubscribeChain()

    _retain_failed_singleton = True

    CONFIG_WATCH = {
        "TRANSFER_THREADS",
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
