"""宿主启动阶段构建的类型化运行时上下文。"""

from collections.abc import AsyncGenerator, Generator
from dataclasses import dataclass
from typing import Protocol

from app.application.messaging.chat import (
    AsyncAgentChatRepository,
    AsyncUnitOfWork,
)
from app.application.outbox import AsyncOutboxTransaction
from app.application.configuration import RuntimeConfiguration, RuntimeSettingsService
from app.application.subscription.delete import SubscribeDeletionRepository
from app.application.subscription.identity import SubscribeIdentityDeletionRepository
from app.application.subscription.mutation import (
    SubscriptionHistoryMutationRepository,
    SubscriptionMutationRepository,
)


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

    def __call__(self, session: object) -> AsyncOutboxTransaction:
        """绑定请求会话并返回 outbox 暂存与收口端口。"""
        ...


class SubscriptionRepositoryFactory(Protocol):
    """由请求会话构造订阅写仓储的工厂。"""

    def __call__(
        self,
        session: object,
    ) -> (
        SubscriptionMutationRepository
        | SubscribeDeletionRepository
        | SubscribeIdentityDeletionRepository
    ):
        """绑定请求会话并返回订阅领域仓储。"""
        ...


class SubscriptionHistoryRepositoryFactory(Protocol):
    """由请求会话构造订阅历史写仓储的工厂。"""

    def __call__(self, session: object) -> SubscriptionHistoryMutationRepository:
        """绑定请求会话并返回订阅历史仓储。"""
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
    standalone_user: StandaloneRepositoryFactory
    system_config: StandaloneRepositoryFactory
    passkey: StandaloneRepositoryFactory


@dataclass(frozen=True, slots=True)
class MessagingRuntime:
    """消息历史 API 的显式仓储工厂。"""

    repository: RepositoryFactory


@dataclass(frozen=True, slots=True)
class HistoryRuntime:
    """下载、整理、媒体服务器与 Dashboard 领域的数据工厂。"""

    download_repository: RepositoryFactory
    transfer_repository: RepositoryFactory
    media_server_repository: RepositoryFactory


@dataclass(frozen=True, slots=True)
class SiteRuntime:
    """站点读写领域的显式仓储工厂。"""

    repository: RepositoryFactory


@dataclass(frozen=True, slots=True)
class WorkflowRuntime:
    """工作流定义、状态与缓存操作所需的数据工厂。"""

    repository: RepositoryFactory
    system_config: StandaloneRepositoryFactory


@dataclass(frozen=True, slots=True)
class SubscriptionRuntime:
    """订阅 API 可见的请求级写事务运行时。"""

    async_session: AsyncSessionProvider
    repository: SubscriptionRepositoryFactory
    history_repository: SubscriptionHistoryRepositoryFactory
    transaction: AsyncUnitOfWorkFactory
    outbox: AsyncOutboxFactory


@dataclass(frozen=True, slots=True)
class HostRuntime:
    """宿主组合根构建且在一个 FastAPI lifespan 内共享的运行时对象。"""

    agent_chat: AgentChatRuntime
    persistence: PersistenceRuntime
    authentication: AuthenticationRuntime
    messaging: MessagingRuntime
    history: HistoryRuntime
    site: SiteRuntime
    subscription: SubscriptionRuntime
    workflow: WorkflowRuntime
    configuration: RuntimeConfiguration
    settings: RuntimeSettingsService
