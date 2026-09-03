"""Chain 兼容门面所需运行时依赖的显式上下文。"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional

from app.application.chain.events import ChainDurableEventWriter
from app.application.configuration import ChainRuntimeConfig
from app.runtime.stop import StopState, runtime_stop_state

if TYPE_CHECKING:
    from app.application.classification.execution import ClassificationExecutionPort
    from app.application.download.failures import DownloadFailureRepository
    from app.application.history import (
        DownloadHistoryRepository,
        TransferHistoryRepository,
    )
    from app.application.mediaserver import MediaServerRepository
    from app.application.messaging.message import MessageHelper, MessageQueueManager
    from app.application.rules import SyncRuleGroupMutationService
    from app.application.security.user import ChainUserRepository
    from app.application.site.contract import SiteRepository
    from app.application.site.mutation import SyncSiteReferenceMutationService
    from app.application.subscription.complete import CompletionScope
    from app.application.subscription.contract import SubscriptionRepository
    from app.application.subscription.delete import (
        DeleteSubscribeScope,
        SyncDeleteSubscribeScope,
    )
    from app.application.subscription.execution import SubscriptionSearchRepository
    from app.application.subscription.mutation import (
        SubscriptionMutationScope,
        SyncSubscriptionMutationScope,
    )
    from app.application.transfer.execution import TransferExecutionRepository
    from app.application.transfer.workflow import TransferAdmissionRepository

ModuleDispatcherFactory = Callable[..., Any]
LegacyTransferCommand = Callable[..., Any]
ChainRuntimeContextProvider = Callable[[], "ChainRuntimeContext"]


@dataclass(frozen=True, slots=True)
class ChainRuntimeContext:
    """集中声明 Chain 调度、事件、消息和缓存所需的最小运行时对象。"""

    module_manager: Any
    plugin_manager: Any
    event_manager: Any
    message_oper: Any
    message_helper: MessageHelper
    file_cache: Any
    async_file_cache: Any
    message_queue: MessageQueueManager
    module_dispatcher_factory: ModuleDispatcherFactory
    site_repository: SiteRepository
    subscription_repository: SubscriptionRepository
    subscription_mutation_scope: SubscriptionMutationScope
    sync_subscription_mutation_scope: SyncSubscriptionMutationScope
    subscription_delete_scope: DeleteSubscribeScope
    sync_subscription_delete_scope: SyncDeleteSubscribeScope
    subscription_completion_scope: CompletionScope
    rule_group_mutation_scope: Callable[
        [], AbstractContextManager[SyncRuleGroupMutationService]
    ]
    site_reference_mutation_scope: Callable[
        [], AbstractContextManager[SyncSiteReferenceMutationService]
    ]
    download_history_repository: DownloadHistoryRepository
    transfer_history_repository: TransferHistoryRepository
    transfer_admission_repository: TransferAdmissionRepository
    transfer_execution_repository: TransferExecutionRepository
    media_server_repository: MediaServerRepository
    download_failure_repository: DownloadFailureRepository
    user_repository: ChainUserRepository
    classification_service: Optional[ClassificationExecutionPort] = None
    subscription_search_repository: Optional[SubscriptionSearchRepository] = None
    legacy_transfer_command: Optional[LegacyTransferCommand] = None
    durable_event_writer: Optional[ChainDurableEventWriter] = None
    configuration: ChainRuntimeConfig = field(
        default_factory=lambda: ChainRuntimeConfig(media_extensions=())
    )
    stop_state: StopState = field(default_factory=lambda: runtime_stop_state)


def _unconfigured_chain_runtime_context() -> ChainRuntimeContext:
    """拒绝在组合根装配前隐式抓取全局管理器。"""
    raise RuntimeError("Chain 运行上下文尚未由启动组合根配置")


_context_provider: ChainRuntimeContextProvider = _unconfigured_chain_runtime_context


def configure_chain_runtime_context_provider(
    provider: Optional[ChainRuntimeContextProvider],
) -> None:
    """由组合根替换 Chain 上下文来源；传入空值恢复未配置状态。"""
    global _context_provider
    _context_provider = provider or _unconfigured_chain_runtime_context


def get_chain_runtime_context() -> ChainRuntimeContext:
    """返回当前组合根提供的 Chain 运行上下文。"""
    return _context_provider()
