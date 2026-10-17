"""
Chain 兼容门面所需运行时依赖的显式上下文。
"""

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
    from app.application.subscription.activity import SubscriptionActivityService
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
    from app.application.system import SystemService
    from app.application.transfer.execution import TransferExecutionRepository
    from app.application.transfer.workflow import TransferAdmissionRepository

ModuleDispatcherFactory = Callable[..., Any]
LegacyTransferCommand = Callable[..., Any]
ChainRuntimeContextProvider = Callable[[], "ChainRuntimeContext"]


@dataclass(frozen=True, slots=True)
class ChainRuntimeContext:
    """
    集中声明 Chain 调度、事件、消息和缓存所需的最小运行时对象。

    :param module_manager: 宿主模块发现与运行状态管理器
    :param plugin_manager: 宿主插件管理器，用于兼容门面查询
    :param event_manager: 进程内事件发布和处理管理器
    :param message_oper: 消息历史持久化操作入口
    :param message_helper: 消息渲染和发送应用服务
    :param file_cache: 链层同步文件缓存入口
    :param async_file_cache: 链层异步文件缓存入口
    :param message_queue: 管理通知排队和投递的消息队列
    :param module_dispatcher_factory: 构造按方法名分发模块调用的调度器工厂
    :param site_repository: 站点配置、用户数据及健康统计仓储
    :param subscription_repository: 订阅查询和独立事务写入仓储
    :param subscription_mutation_scope: 创建异步订阅修改服务及共享事务的作用域
    :param sync_subscription_mutation_scope: 创建同步订阅修改服务及共享事务的作用域
    :param subscription_delete_scope: 创建异步订阅删除事务的作用域
    :param sync_subscription_delete_scope: 创建同步订阅删除事务的作用域
    :param subscription_completion_scope: 创建订阅完成、历史与副作用共同提交的作用域
    :param rule_group_mutation_scope: 创建规则组及订阅引用同步修改事务的作用域
    :param site_reference_mutation_scope: 创建站点引用同步清理事务的作用域
    :param download_history_repository: 下载历史和关联文件的仓储
    :param transfer_history_repository: 整理历史查询与写入仓储
    :param transfer_admission_repository: 持久整理任务准入及排队仓储
    :param transfer_execution_repository: 持久整理任务认领、检查点与结算仓储
    :param media_server_repository: 媒体服务器同步数据读取仓储
    :param download_failure_repository: 下载失败冷却状态仓储
    :param user_repository: 链层读取本地用户归属和权限的仓储
    :param subscription_activity: 订阅活动记录服务；None 表示未装配
    :param classification_service: 完整媒体结果分类执行服务；None 表示未装配
    :param subscription_search_repository: 持久订阅搜索任务仓储；None 表示未装配
    :param legacy_transfer_command: 兼容旧整理调用的命令；None 表示未提供
    :param durable_event_writer: 业务记录与事件投递意图原子写入端口；None 表示未装配
    :param configuration: 未提供动态读取器时使用的配置快照，默认媒体扩展名为空
    :param configuration_provider: 按调用读取最新配置的函数；None 使用静态快照
    :param stop_state: 查询进程停止请求的状态对象，默认使用宿主全局状态
    :param system_service: 系统操作应用服务；None 表示未装配
    """

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
    subscription_activity: Optional[SubscriptionActivityService] = None
    classification_service: Optional[ClassificationExecutionPort] = None
    subscription_search_repository: Optional[SubscriptionSearchRepository] = None
    legacy_transfer_command: Optional[LegacyTransferCommand] = None
    durable_event_writer: Optional[ChainDurableEventWriter] = None
    configuration: ChainRuntimeConfig = field(
        default_factory=lambda: ChainRuntimeConfig(media_extensions=())
    )
    configuration_provider: Optional[Callable[[], ChainRuntimeConfig]] = None
    stop_state: StopState = field(default_factory=lambda: runtime_stop_state)
    system_service: Optional[SystemService] = None


def _unconfigured_chain_runtime_context() -> ChainRuntimeContext:
    """
    拒绝在组合根装配前隐式抓取全局管理器。
    """
    raise RuntimeError("Chain 运行上下文尚未由启动组合根配置")


_context_provider: ChainRuntimeContextProvider = _unconfigured_chain_runtime_context


def configure_chain_runtime_context_provider(
    provider: Optional[ChainRuntimeContextProvider],
) -> None:
    """
    由组合根替换 Chain 上下文来源；传入空值恢复未配置状态。
    """
    global _context_provider
    _context_provider = provider or _unconfigured_chain_runtime_context


def get_chain_runtime_context() -> ChainRuntimeContext:
    """
    返回当前组合根提供的 Chain 运行上下文。
    """
    return _context_provider()
