"""
宿主启动阶段构建的类型化运行时上下文。
"""

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
from app.application.subscription.activity import SubscriptionActivityRepository
from app.application.subscription.contract import (
    SessionSubscriptionPort,
    SubscriptionHistoryStagingPort,
    SubscriptionSnapshot,
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
from app.schemas.types import MediaSource

if TYPE_CHECKING:
    from app.application.messaging.message import MessageHelper, MessageQueueManager


class AgentChatRepositoryFactory(Protocol):
    """
    由请求会话构造 Agent 会话仓储的工厂端口。
    """

    def __call__(self, session: object) -> AsyncAgentChatRepository:
        """
        绑定请求会话并返回 Agent 会话仓储。

        :param session: 当前请求持有的数据库会话
        :return: 使用该会话读写聊天记录的仓储
        """
        ...


class AsyncUnitOfWorkFactory(Protocol):
    """
    由请求会话构造异步事务端口的工厂。
    """

    def __call__(self, session: object) -> AsyncUnitOfWork:
        """
        绑定请求会话并返回异步事务端口。

        :param session: 当前请求持有的数据库会话
        :return: 控制该会话提交和回滚的事务端口
        """
        ...


class AsyncOutboxFactory(Protocol):
    """
    由请求会话构造异步 outbox 事务端口的工厂。
    """

    def __call__(self, session: object) -> AsyncOutboxStager:
        """
        绑定请求会话并返回 outbox 暂存与收口端口。

        :param session: 当前请求持有的数据库会话
        :return: 与业务写入共享会话的 outbox 端口
        """
        ...


class RequestSubscriptionRepository(SessionSubscriptionPort, Protocol):
    """
    请求会话仓储提供的订阅读写和 Servarr 兼容查询能力。
    """

    def list_by_media_identity(
        self,
        media_source: MediaSource,
        media_id: str,
        music_type: str | None = None,
    ) -> list[SubscriptionSnapshot]:
        """
        按规范媒体身份同步读取订阅快照。

        :param media_source: 媒体所属来源
        :param media_id: 来源内的媒体标识
        :param music_type: 音乐媒体的细分类型；非音乐时为 None
        :return: 符合媒体身份的订阅快照列表
        """
        ...

    async def async_exists(
        self,
        media_source: MediaSource,
        media_id: str,
        season: int | None = None,
        episode_group: str | None = None,
        music_type: str | None = None,
        mtype: str | None = None,
    ) -> SubscriptionSnapshot | None:
        """
        按媒体身份、季和剧集组异步读取命中的订阅快照。

        :param media_source: 媒体所属来源
        :param media_id: 来源内的媒体标识
        :param season: 限定的季号；None 表示不按季过滤
        :param episode_group: 限定的剧集组；None 表示不按组过滤
        :param music_type: 音乐媒体的细分类型；非音乐时为 None
        :param mtype: 媒体类型；None 表示不按类型过滤
        :return: 命中的订阅快照；不存在时为 None
        """
        ...

    async def async_delete(self, subscribe_id: int) -> None:
        """
        在当前请求事务中暂存删除，不自行提交。

        :param subscribe_id: 待删除订阅的数据库主键
        """
        ...


class SubscriptionRepositoryFactory(Protocol):
    """
    由请求会话构造订阅读写仓储的工厂。
    """

    def __call__(
        self,
        session: object,
    ) -> RequestSubscriptionRepository:
        """
        绑定请求会话并返回订阅领域仓储。

        :param session: 当前请求持有的数据库会话
        :return: 使用该会话读写订阅的仓储
        """
        ...


class SubscriptionHistoryRepositoryFactory(Protocol):
    """
    由请求会话构造订阅历史写仓储的工厂。
    """

    def __call__(self, session: object) -> SubscriptionHistoryStagingPort:
        """
        绑定请求会话并返回订阅历史仓储。

        :param session: 当前请求持有的数据库会话
        :return: 使用该会话暂存历史记录的仓储
        """
        ...


class SubscriptionBatchWriterFactory(Protocol):
    """
    由请求事务组件构造原子批量订阅写端口的工厂。
    """

    def __call__(
        self,
        *,
        repository: SubscriptionStagingPort,
        unit_of_work: SubscriptionAsyncUnitOfWork,
        outbox: AsyncSubscriptionOutboxStager,
        dispatch_store: AsyncOutboxDispatchStore,
    ) -> SubscriptionBatchWritePort:
        """
        组合共享 Session、UoW 与 outbox 并返回批量写端口。

        :param repository: 暂存订阅记录变更的仓储
        :param unit_of_work: 控制共享会话提交与回滚的事务端口
        :param outbox: 在同一事务内暂存待发送事件的端口
        :param dispatch_store: 查询和更新待分发事件的存储端口
        :return: 在同一事务中执行批量订阅写入的端口
        """
        ...


class SubscriptionExecutionStatusRepositoryFactory(Protocol):
    """
    由请求会话构造订阅执行状态读取仓储的工厂。
    """

    def __call__(self, session: object) -> object:
        """
        绑定请求会话并返回执行状态读取端口。

        :param session: 当前请求持有的数据库会话
        :return: 使用该会话查询订阅执行状态的端口
        """
        ...


class AsyncSessionProvider(Protocol):
    """
    FastAPI 请求级异步会话提供器。
    """

    def __call__(self) -> AsyncGenerator[object, None]:
        """
        生成一个请求独占的异步数据库会话。

        :yields: 请求期间使用的异步会话，退出时由提供器释放
        """
        ...


class SyncSessionProvider(Protocol):
    """
    兼容 API Facade 使用的同步会话提供器。
    """

    def __call__(self) -> Generator[object, None, None]:
        """
        生成一个请求独占的同步数据库会话。

        :yields: 请求期间使用的同步会话，退出时由提供器释放
        """
        ...


class RepositoryFactory(Protocol):
    """
    由请求 Session 构造某一明确领域仓储的通用工厂。
    """

    def __call__(self, session: object) -> object:
        """
        绑定请求会话并返回领域仓储。

        :param session: 当前请求持有的数据库会话
        :return: 由调用点约定具体领域类型的仓储
        """
        ...


class TransferHistoryRepositoryFactory(Protocol):
    """
    由请求 Session 构造整理历史事务仓储的工厂。
    """

    def __call__(self, session: object) -> TransferHistoryMutationRepository:
        """
        绑定请求会话并返回整理历史暂存仓储。

        :param session: 当前请求持有的数据库会话
        :return: 使用该会话暂存整理历史的仓储
        """
        ...


class StandaloneRepositoryFactory(Protocol):
    """
    构造自持有兼容事务边界的领域仓储。
    """

    def __call__(self) -> object:
        """
        返回无需请求 Session 的领域仓储。

        :return: 自行管理事务边界的兼容仓储
        """
        ...


class SyncUnitOfWorkFactory(Protocol):
    """
    由同步请求 Session 构造事务端口的工厂。
    """

    def __call__(self, session: object) -> object:
        """
        绑定请求会话并返回同步事务端口。

        :param session: 当前请求持有的同步数据库会话
        :return: 控制该会话提交和回滚的事务端口
        """
        ...


@dataclass(frozen=True, slots=True)
class AgentChatRuntime:
    """
    Agent 会话 API 可见的最小数据运行时。

    :param async_session: 请求级异步会话提供器
    :param repository: 将当前会话绑定为 Agent 聊天仓储的工厂
    :param transaction: 将当前会话绑定为异步事务端口的工厂
    :param persistence: Agent 聊天消息持久化服务
    """

    async_session: AsyncSessionProvider
    repository: AgentChatRepositoryFactory
    transaction: AsyncUnitOfWorkFactory
    persistence: AgentChatPersistenceService


@dataclass(frozen=True, slots=True)
class PersistenceRuntime:
    """
    全部 HTTP 业务领域共享的请求会话与事务工厂。

    :param sync_session: 请求级同步数据库会话提供器
    :param async_session: 请求级异步数据库会话提供器
    :param sync_transaction: 为当前同步会话提供提交和回滚能力的工厂
    :param async_transaction: 为当前异步会话提供提交和回滚能力的工厂
    """

    sync_session: SyncSessionProvider
    async_session: AsyncSessionProvider
    sync_transaction: SyncUnitOfWorkFactory
    async_transaction: AsyncUnitOfWorkFactory


@dataclass(frozen=True, slots=True)
class AuthenticationRuntime:
    """
    认证、用户管理与 PassKey API 的显式数据工厂。

    :param user_repository: 将请求会话绑定为用户仓储的工厂
    :param passkey_repository: 将请求会话绑定为通行密钥仓储的工厂
    :param standalone_user: 构造独立用户仓储的工厂
    :param system_config: 构造独立系统配置访问端口的工厂
    :param passkey: 构造独立通行密钥访问端口的工厂
    """

    user_repository: RepositoryFactory
    passkey_repository: RepositoryFactory
    standalone_user: StandaloneRepositoryFactory
    system_config: StandaloneRepositoryFactory
    passkey: StandaloneRepositoryFactory


@dataclass(frozen=True, slots=True)
class MessagingRuntime:
    """
    消息历史 API 的显式仓储工厂。

    :param repository: 按请求会话构造消息历史仓储的工厂
    :param helper: 负责消息查询及发送协调的应用门面
    :param queue: 拥有后台消息处理任务的队列管理器
    """

    repository: RepositoryFactory
    helper: MessageHelper
    queue: MessageQueueManager


@dataclass(frozen=True, slots=True)
class HistoryRuntime:
    """
    下载、整理、媒体服务器与 Dashboard 领域的数据工厂。

    :param download_repository: 构造下载历史仓储的工厂
    :param transfer_repository: 整理历史查询仓储
    :param transfer_mutation_repository: 按请求会话构造整理历史写仓储的工厂
    :param media_server_repository: 构造媒体服务器条目仓储的工厂
    :param transfer_execution_repository: 整理执行状态仓储
    """

    download_repository: RepositoryFactory
    transfer_repository: TransferHistoryRepository
    transfer_mutation_repository: TransferHistoryRepositoryFactory
    media_server_repository: RepositoryFactory
    transfer_execution_repository: TransferExecutionRepository


@dataclass(frozen=True, slots=True)
class SiteRuntime:
    """
    站点读写领域的显式仓储工厂。

    :param repository: 将当前请求会话绑定为站点仓储的工厂
    :param standalone: 供独立读写操作使用的站点仓储
    """

    repository: RepositoryFactory
    standalone: SiteRepository


@dataclass(frozen=True, slots=True)
class WorkflowRuntime:
    """
    工作流定义、状态与缓存操作所需的数据工厂。

    :param query: 工作流定义和运行结果查询服务
    :param repository: 按请求会话构造工作流仓储的工厂
    :param system_config: 构造工作流配置缓存端口的工厂
    """

    query: WorkflowQueryService
    repository: RepositoryFactory
    system_config: Callable[[], WorkflowCachePort]


@dataclass(frozen=True, slots=True)
class SubscriptionRuntime:
    """
    订阅 API 可见的请求级写事务运行时。

    :param async_session: 一个请求独占的异步数据库会话提供器
    :param repository: 将当前会话绑定为订阅读写仓储的工厂
    :param history_repository: 将当前会话绑定为订阅历史暂存仓储的工厂
    :param transaction: 当前会话的异步提交与回滚端口工厂
    :param outbox: 当前会话的持久事件暂存端口工厂
    :param dispatch_store: 事务提交后分发已持久化事件的存储端口
    :param batch_writer: 构造同事务批量订阅写入端口的工厂
    :param rule_group_mutation_scope: 为同步规则组变更创建独占事务范围的工厂
    :param async_rule_group_mutation_scope: 为异步规则组变更创建独占事务范围的工厂
    :param site_reference_mutation_scope: 为站点关联关系变更创建独占事务范围的工厂
    :param execution_status_repository: 可选的订阅执行状态仓储工厂；None 表示未接入
    :param search_repository: 可选的订阅搜索仓储；None 表示未接入
    :param activity_repository: 可选的订阅活动仓储；None 表示未接入
    """

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
    activity_repository: SubscriptionActivityRepository | None = None


@dataclass(frozen=True, slots=True)
class HostRuntime:
    """
    宿主组合根构建且在一个 FastAPI lifespan 内共享的运行时对象。

    :param agent_chat: 请求级 Agent 聊天持久化能力
    :param agent: Agent 数据访问与行为上下文
    :param persistence: 请求级数据库会话和事务工厂
    :param authentication: 用户与通行密钥数据端口
    :param messaging: 消息历史和队列能力
    :param history: 下载、整理和媒体服务器历史数据端口
    :param site: 站点数据端口
    :param subscription: 订阅请求级事务端口
    :param workflow: 工作流查询和配置端口
    :param classification: 媒体分类规则运行时
    :param classification_execution: 分类执行端口
    :param system: 系统配置和状态服务
    :param configuration: 进程级配置快照
    :param settings: 可读写的系统设置服务
    :param tasks: 进程级后台任务登记器
    """

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
