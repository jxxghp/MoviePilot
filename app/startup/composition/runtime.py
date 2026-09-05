"""宿主运行时、领域投影与共享依赖的唯一组合根。"""

from __future__ import annotations

from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Callable, cast

from app.api.data import (
    ApiDataPorts,
    configure_api_data_runtime,
    reset_api_data_runtime,
)
from app.application.history import (
    DownloadHistoryRepository,
    TransferHistoryRepository,
    configure_transfer_history_repository,
    reset_transfer_history_repository,
)
from app.application.site.contract import SiteRepository
from app.application.site.health import (
    SiteHealthService,
    configure_site_health_service,
    reset_site_health_service,
)
from app.application.site.query import (
    SiteQueryService,
    configure_site_query_service,
    reset_site_query_service,
)
from app.application.subscription.contract import (
    SubscriptionHistoryQueryPort,
    SubscriptionRepository,
)
from app.application.transfer.execution import TransferExecutionRepository
from app.runtime.tasks import TaskRegistry
from app.startup.composition.context import (
    AgentChatRuntime,
    AuthenticationRuntime,
    HistoryRuntime,
    HostRuntime,
    MessagingRuntime,
    PersistenceRuntime,
    RepositoryFactory,
    SiteRuntime,
    SubscriptionRuntime,
    WorkflowRuntime,
)

if TYPE_CHECKING:
    from app.application.classification.execution import ClassificationExecutionPort
    from app.application.classification.runtime import ClassificationRuntime
    from app.application.messaging.message import MessageHelper, MessageQueueManager
    from app.application.subscription.execution import SubscriptionSearchRepository
    from app.startup.composition.agent import AgentComposition
    from app.startup.composition.configuration import ConfigurationComposition
    from app.startup.composition.database import DatabaseComposition
    from app.startup.composition.security import SecurityComposition


@dataclass(frozen=True, slots=True)
class RuntimeDependencies:
    """保存 HostRuntime、Agent 与 Chain 复用的同一批有状态依赖。"""

    download_history: DownloadHistoryRepository
    transfer_history: TransferHistoryRepository
    site: SiteRepository
    subscription: SubscriptionRepository
    subscription_history: SubscriptionHistoryQueryPort
    transfer_execution: TransferExecutionRepository
    message_helper: MessageHelper
    message_queue: MessageQueueManager
    subscription_search: SubscriptionSearchRepository | None = None


@dataclass(frozen=True, slots=True)
class RuntimeInputs:
    """描述构造 HostRuntime 所需的已完成组合输入。"""

    configuration: ConfigurationComposition
    database: DatabaseComposition
    agent: AgentComposition
    authentication: SecurityComposition
    classification: ClassificationRuntime
    classification_execution: ClassificationExecutionPort
    dependencies: RuntimeDependencies
    tasks: TaskRegistry


@dataclass(frozen=True, slots=True)
class RuntimeComposition:
    """保存宿主运行时及其兼容与应用服务投影。"""

    runtime: HostRuntime
    dependencies: RuntimeDependencies
    api_data: ApiDataPorts
    site_query: SiteQueryService
    site_health: SiteHealthService


def compose_runtime_dependencies() -> RuntimeDependencies:
    """构造一个 lifespan 内由 Agent、Chain 与 HostRuntime 共享的依赖。"""
    # 具体 DB adapter 延迟到执行期加载，导入组合 owner 不得启动或牵入数据库 worker。
    from app.application.messaging.message import MessageHelper, MessageQueueManager
    from app.db.adapters.history.download import TransactionalDownloadHistoryRepository
    from app.db.adapters.history.transfer import TransactionalTransferHistoryRepository
    from app.db.adapters.site import TransactionalSiteRepository
    from app.db.adapters.subscription import (
        TransactionalSubscriptionHistoryRepository,
        TransactionalSubscriptionRepository,
    )
    from app.db.adapters.subscriptionsearch import TransactionalSubscriptionSearchRepository
    from app.db.adapters.transfer.execution import (
        TransactionalTransferExecutionRepository,
    )
    from app.db.session import SessionFactory, async_session_scope

    message_helper_factory: Callable[[], MessageHelper] = MessageHelper
    return RuntimeDependencies(
        download_history=TransactionalDownloadHistoryRepository(
            sync_session=SessionFactory,
            async_session=async_session_scope,
        ),
        transfer_history=TransactionalTransferHistoryRepository(
            sync_session=SessionFactory,
            async_session=async_session_scope,
        ),
        site=TransactionalSiteRepository(
            sync_session=SessionFactory,
            async_session=async_session_scope,
        ),
        subscription=TransactionalSubscriptionRepository(
            sync_session=SessionFactory,
            async_session=async_session_scope,
        ),
        subscription_history=TransactionalSubscriptionHistoryRepository(
            async_session=async_session_scope,
        ),
        transfer_execution=TransactionalTransferExecutionRepository(SessionFactory),
        message_helper=message_helper_factory(),
        message_queue=MessageQueueManager(auto_start=False),
        subscription_search=TransactionalSubscriptionSearchRepository(
            SessionFactory,
            async_session_scope,
        ),
    )


def compose_runtime(inputs: RuntimeInputs) -> RuntimeComposition:
    """从同一批组合输入构造 HostRuntime 及旧 API 数据投影。"""
    # 领域 Runtime 的具体持久化投影只在组合阶段解析，保持模块冷导入合同。
    from app.db.adapters.history.download import SessionDownloadHistoryRepository
    from app.db.adapters.history.transfer import SessionTransferHistoryRepository
    from app.db.adapters.outbox import (
        SqlAlchemyAsyncOutboxDispatchStore,
        SqlAlchemyAsyncOutboxStager,
    )
    from app.db.adapters.site import SessionSiteRepository
    from app.db.adapters.subscription import (
        SessionSubscriptionHistoryRepository,
        SessionSubscriptionRepository,
    )
    from app.db.adapters.subscriptionstatus import (
        SessionSubscriptionExecutionStatusRepository,
    )
    from app.db.oper.mediaserver import MediaServerOper
    from app.db.oper.message import MessageOper
    from app.db.oper.workflow import WorkflowOper
    from app.db.session import async_session_scope, get_async_db, get_db
    from app.db.uow import SqlAlchemyAsyncUnitOfWork, SqlAlchemyUnitOfWork
    from app.startup.composition.subscription import (
        async_rule_group_mutation_scope,
        build_subscription_batch_writer,
        rule_group_mutation_scope,
        site_reference_mutation_scope,
    )
    from app.startup.composition.system import compose_system_service

    dependencies = inputs.dependencies
    configuration = inputs.configuration
    authentication = AuthenticationRuntime(
        user_repository=inputs.authentication.user_repository,
        passkey_repository=inputs.authentication.passkey_repository,
        standalone_user=inputs.authentication.standalone_user,
        system_config=inputs.authentication.system_config,
        passkey=inputs.authentication.passkey,
    )
    async_rule_scope = partial(
        async_rule_group_mutation_scope,
        configuration.system_config.publish_many,
    )
    host_runtime = HostRuntime(
        agent_chat=AgentChatRuntime(
            async_session=get_async_db,
            repository=inputs.agent.chat_repository,
            transaction=SqlAlchemyAsyncUnitOfWork,
            persistence=inputs.agent.persistence,
        ),
        agent=inputs.agent.data,
        persistence=PersistenceRuntime(
            sync_session=get_db,
            async_session=get_async_db,
            sync_transaction=SqlAlchemyUnitOfWork,
            async_transaction=SqlAlchemyAsyncUnitOfWork,
        ),
        authentication=authentication,
        messaging=MessagingRuntime(
            repository=cast(RepositoryFactory, MessageOper),
            helper=dependencies.message_helper,
            queue=dependencies.message_queue,
        ),
        history=HistoryRuntime(
            download_repository=SessionDownloadHistoryRepository,
            transfer_repository=dependencies.transfer_history,
            transfer_mutation_repository=SessionTransferHistoryRepository,
            media_server_repository=cast(RepositoryFactory, MediaServerOper),
            transfer_execution_repository=dependencies.transfer_execution,
        ),
        site=SiteRuntime(
            repository=SessionSiteRepository,
            standalone=dependencies.site,
        ),
        subscription=SubscriptionRuntime(
            async_session=get_async_db,
            repository=SessionSubscriptionRepository,
            history_repository=SessionSubscriptionHistoryRepository,
            execution_status_repository=SessionSubscriptionExecutionStatusRepository,
            search_repository=dependencies.subscription_search,
            transaction=SqlAlchemyAsyncUnitOfWork,
            outbox=SqlAlchemyAsyncOutboxStager,
            dispatch_store=SqlAlchemyAsyncOutboxDispatchStore(async_session_scope),
            batch_writer=build_subscription_batch_writer,
            rule_group_mutation_scope=partial(
                rule_group_mutation_scope,
                configuration.system_config.publish_many,
            ),
            async_rule_group_mutation_scope=async_rule_scope,
            site_reference_mutation_scope=partial(
                site_reference_mutation_scope,
                configuration.system_config.publish_many,
            ),
        ),
        workflow=WorkflowRuntime(
            query=inputs.database.workflow_query,
            repository=cast(RepositoryFactory, WorkflowOper),
            system_config=lambda: configuration.system_service,
        ),
        classification=inputs.classification,
        classification_execution=inputs.classification_execution,
        system=compose_system_service(
            settings=configuration.settings,
            system_config=configuration.system_service,
            rule_group_mutation=async_rule_scope,
        ),
        configuration=configuration.runtime,
        settings=configuration.settings,
        tasks=inputs.tasks,
    )
    api_data = ApiDataPorts(
        sync_session=host_runtime.persistence.sync_session,
        async_session=host_runtime.persistence.async_session,
        repositories={
            "download_history": host_runtime.history.download_repository,
            "media_server": host_runtime.history.media_server_repository,
            "message": host_runtime.messaging.repository,
            "passkey": host_runtime.authentication.passkey_repository,
            "site": host_runtime.site.repository,
            "subscribe": host_runtime.subscription.repository,
            "subscribe_history": host_runtime.subscription.history_repository,
            "user": host_runtime.authentication.user_repository,
            "workflow": host_runtime.workflow.repository,
        },
        standalone={
            "passkey": host_runtime.authentication.passkey,
            "system_config": host_runtime.authentication.system_config,
            "user": host_runtime.authentication.standalone_user,
        },
        unit_of_work={
            "async": host_runtime.persistence.async_transaction,
            "sync": host_runtime.persistence.sync_transaction,
        },
    )
    return RuntimeComposition(
        runtime=host_runtime,
        dependencies=dependencies,
        api_data=api_data,
        site_query=SiteQueryService(repository=dependencies.site),
        site_health=SiteHealthService(repository=dependencies.site),
    )


def publish_runtime(composition: RuntimeComposition) -> None:
    """发布 HostRuntime 派生的兼容端口与共享领域服务。"""
    configure_api_data_runtime(composition.api_data)
    configure_transfer_history_repository(lambda: composition.dependencies.transfer_history)
    configure_site_query_service(composition.site_query)
    configure_site_health_service(composition.site_health)


def reset_runtime() -> None:
    """撤销当前 lifespan 由运行时组合根发布的全部投影。"""
    reset_site_health_service()
    reset_site_query_service()
    reset_transfer_history_repository()
    reset_api_data_runtime()
