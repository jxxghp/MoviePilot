"""宿主启动阶段构建的类型化运行时上下文。"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable, Generator
from contextlib import AbstractAsyncContextManager, AbstractContextManager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from app.application.agent import AgentDataContext
from app.application.classification.execution import ClassificationExecutionPort
from app.application.classification.runtime import ClassificationRuntime
from app.application.configuration import RuntimeConfiguration, RuntimeSettingsService
from app.application.history import (
    TransferHistoryMutationRepository,
    TransferHistoryRepository,
)
from app.application.messaging.chat import (
    AgentChatPersistenceService,
    AsyncAgentChatRepository,
    AsyncUnitOfWork,
)
from app.application.outbox import AsyncOutboxDispatchStore, AsyncOutboxStager
from app.application.rules import AsyncRuleGroupMutationService, SyncRuleGroupMutationService
from app.application.site.contract import SiteRepository
from app.application.site.mutation import SyncSiteReferenceMutationService
from app.application.subscription.contract import (
    SubscriptionHistoryStagingPort,
    SubscriptionStagingPort,
)
from app.application.subscription.write import (
    AsyncSubscriptionOutboxStager,
    SubscriptionBatchWritePort,
)
from app.application.subscription.write import (
    AsyncUnitOfWork as SubscriptionAsyncUnitOfWork,
)
from app.application.system import SystemService
from app.application.transfer.execution import TransferExecutionRepository
from app.application.workflow import WorkflowCachePort, WorkflowQueryService
from app.runtime.tasks import TaskRegistry

if TYPE_CHECKING:
    from app.application.messaging.message import MessageHelper, MessageQueueManager


class AgentChatRepositoryFactory(Protocol):
    """由请求会话构造 Agent 会话仓储的工厂端口。"""

    def __call__(self, session: object) -> AsyncAgentChatRepository:
        """绑定请求会话并返回 Agent 会话仓储。"""
        ...


class AsyncUnitOfWorkFactory(Protocol):
    """由请求会话构造异步事务端口的工厂。"""

    def __call__(self, session: object) -> AsyncUnitOfWork:
        """绑定请求会话并返回异步事务端口。"""
        ...


class AsyncOutboxFactory(Protocol):
    """由请求会话构造异步 outbox 事务端口的工厂。"""

    def __call__(self, session: object) -> AsyncOutboxStager:
        """绑定请求会话并返回 outbox 暂存与收口端口。"""
        ...


class SubscriptionRepositoryFactory(Protocol):
    """由请求会话构造订阅写仓储的工厂。"""

    def __call__(
        self,
        session: object,
    ) -> SubscriptionStagingPort:
        """绑定请求会话并返回订阅领域仓储。"""
        ...


class SubscriptionHistoryRepositoryFactory(Protocol):
    """由请求会话构造订阅历史写仓储的工厂。"""

    def __call__(self, session: object) -> SubscriptionHistoryStagingPort:
        """绑定请求会话并返回订阅历史仓储。"""
        ...


class SubscriptionBatchWriterFactory(Protocol):
    """由请求事务组件构造原子批量订阅写端口的工厂。"""

    def __call__(
        self,
        *,
        repository: SubscriptionStagingPort,
        unit_of_work: SubscriptionAsyncUnitOfWork,
        outbox: AsyncSubscriptionOutboxStager,
        dispatch_store: AsyncOutboxDispatchStore,
    ) -> SubscriptionBatchWritePort:
        """组合共享 Session、UoW 与 outbox 并返回批量写端口。"""
        ...


class SubscriptionExecutionStatusRepositoryFactory(Protocol):
    """由请求会话构造订阅执行状态读取仓储的工厂。"""

    def __call__(self, session: object) -> object:
        """绑定请求会话并返回执行状态读取端口。"""
        ...


class AsyncSessionProvider(Protocol):
    """FastAPI 请求级异步会话提供器。"""

    def __call__(self) -> AsyncGenerator[object, None]:
        """生成一个请求独占的异步数据库会话。"""
        ...


class SyncSessionProvider(Protocol):
    """兼容 API Facade 使用的同步会话提供器。"""

    def __call__(self) -> Generator[object, None, None]:
        """生成一个请求独占的同步数据库会话。"""
        ...


class RepositoryFactory(Protocol):
    """由请求 Session 构造某一明确领域仓储的通用工厂。"""

    def __call__(self, session: object) -> object:
        """绑定请求会话并返回领域仓储。"""
        ...


class TransferHistoryRepositoryFactory(Protocol):
    """由请求 Session 构造整理历史事务仓储的工厂。"""

    def __call__(self, session: object) -> TransferHistoryMutationRepository:
        """绑定请求会话并返回整理历史暂存仓储。"""
        ...


class StandaloneRepositoryFactory(Protocol):
    """构造自持有兼容事务边界的领域仓储。"""

    def __call__(self) -> object:
        """返回无需请求 Session 的领域仓储。"""
        ...


class SyncUnitOfWorkFactory(Protocol):
    """由同步请求 Session 构造事务端口的工厂。"""

    def __call__(self, session: object) -> object:
        """绑定请求会话并返回同步事务端口。"""
        ...


@dataclass(frozen=True, slots=True)
class AgentChatRuntime:
    """Agent 会话 API 可见的最小数据运行时。"""

    async_session: AsyncSessionProvider
    repository: AgentChatRepositoryFactory
    transaction: AsyncUnitOfWorkFactory
    persistence: AgentChatPersistenceService


@dataclass(frozen=True, slots=True)
class PersistenceRuntime:
    """全部 HTTP 业务领域共享的请求会话与事务工厂。"""

    sync_session: SyncSessionProvider
    async_session: AsyncSessionProvider
    sync_transaction: SyncUnitOfWorkFactory
    async_transaction: AsyncUnitOfWorkFactory


@dataclass(frozen=True, slots=True)
class AuthenticationRuntime:
    """认证、用户管理与 PassKey API 的显式数据工厂。"""

    user_repository: RepositoryFactory
    passkey_repository: RepositoryFactory
    standalone_user: StandaloneRepositoryFactory
    system_config: StandaloneRepositoryFactory
    passkey: StandaloneRepositoryFactory


@dataclass(frozen=True, slots=True)
class MessagingRuntime:
    """消息历史 API 的显式仓储工厂。"""

    repository: RepositoryFactory
    helper: MessageHelper
    queue: MessageQueueManager


@dataclass(frozen=True, slots=True)
class HistoryRuntime:
    """下载、整理、媒体服务器与 Dashboard 领域的数据工厂。"""

    download_repository: RepositoryFactory
    transfer_repository: TransferHistoryRepository
    transfer_mutation_repository: TransferHistoryRepositoryFactory
    media_server_repository: RepositoryFactory
    transfer_execution_repository: TransferExecutionRepository


@dataclass(frozen=True, slots=True)
class SiteRuntime:
    """站点读写领域的显式仓储工厂。"""

    repository: RepositoryFactory
    standalone: SiteRepository


@dataclass(frozen=True, slots=True)
class WorkflowRuntime:
    """工作流定义、状态与缓存操作所需的数据工厂。"""

    query: WorkflowQueryService
    repository: RepositoryFactory
    system_config: Callable[[], WorkflowCachePort]


@dataclass(frozen=True, slots=True)
class SubscriptionRuntime:
    """订阅 API 可见的请求级写事务运行时。"""

    async_session: AsyncSessionProvider
    repository: SubscriptionRepositoryFactory
    history_repository: SubscriptionHistoryRepositoryFactory
    transaction: AsyncUnitOfWorkFactory
    outbox: AsyncOutboxFactory
    dispatch_store: AsyncOutboxDispatchStore
    batch_writer: SubscriptionBatchWriterFactory
    rule_group_mutation_scope: Callable[[], AbstractContextManager[SyncRuleGroupMutationService]]
    async_rule_group_mutation_scope: Callable[[], AbstractAsyncContextManager[AsyncRuleGroupMutationService]]
    site_reference_mutation_scope: Callable[[], AbstractContextManager[SyncSiteReferenceMutationService]]
    execution_status_repository: SubscriptionExecutionStatusRepositoryFactory | None = None
    search_repository: object | None = None


@dataclass(frozen=True, slots=True)
class HostRuntime:
    """宿主组合根构建且在一个 FastAPI lifespan 内共享的运行时对象。"""

    agent_chat: AgentChatRuntime
    agent: AgentDataContext
    persistence: PersistenceRuntime
    authentication: AuthenticationRuntime
    messaging: MessagingRuntime
    history: HistoryRuntime
    site: SiteRuntime
    subscription: SubscriptionRuntime
    workflow: WorkflowRuntime
    classification: ClassificationRuntime
    classification_execution: ClassificationExecutionPort
    system: SystemService
    configuration: RuntimeConfiguration
    settings: RuntimeSettingsService
    tasks: TaskRegistry
