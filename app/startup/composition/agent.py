"""Agent 数据、会话与自主任务服务的宿主组合根。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from typing import cast

from app.application.agent import AgentDataContext
from app.application.agenttask import (
    AgentTaskExecutionService,
    configure_agent_task_execution,
)
from app.application.history import DownloadHistoryRepository, TransferHistoryRepository
from app.application.messaging.chat import (
    AgentChatPersistenceService,
    AgentChatService,
    configure_agent_chat_persistence,
    configure_agent_chat_service,
)
from app.application.site.contract import SiteRepository
from app.application.subscription.contract import (
    SubscriptionHistoryQueryPort,
    SubscriptionRepository,
)
from app.application.transfer.execution import TransferExecutionRepository
from app.db.adapters.agent import (
    SessionAgentTaskRepository,
    TransactionalAgentTaskRepository,
    TransactionalPluginDataRepository,
)
from app.db.oper.agentchat import AgentChatOper
from app.db.oper.systemconfig import SystemConfigOper
from app.db.session import SessionFactory, async_session_scope, get_async_db
from app.db.uow import SqlAlchemyAsyncUnitOfWork
from app.startup.composition.context import (
    AgentChatRepositoryFactory,
    AgentChatRuntime,
)
from app.startup.composition.database import (
    DatabaseRuntime,
    build_transactional_user_repository,
)
from app.startup.composition.subscription import (
    async_rule_group_mutation_scope,
    delete_subscribe_scope,
    subscription_mutation_scope,
)

AgentDataContextRegistrar = Callable[[AgentDataContext], None]


@dataclass(frozen=True, slots=True)
class AgentComposition:
    """保存一个 lifespan 内共享的 Agent 数据、会话与任务对象。"""

    data: AgentDataContext
    chat: AgentChatRuntime
    tasks: TransactionalAgentTaskRepository
    execution: AgentTaskExecutionService


def compose_agent(
    *,
    runtime: DatabaseRuntime,
    system_config: SystemConfigOper,
    site: SiteRepository,
    subscription: SubscriptionRepository,
    subscription_history: SubscriptionHistoryQueryPort,
    transfer_history: TransferHistoryRepository,
    transfer_execution: TransferExecutionRepository,
    download_history: DownloadHistoryRepository,
) -> AgentComposition:
    """在数据库 worker 启动后构造共享的 Agent 数据与任务服务。"""
    persistence = AgentChatPersistenceService(
        repository=AgentChatOper,
        async_executor=runtime.worker,
        sync_transaction=runtime.transaction.sync,
        capacity=runtime.worker.snapshot().capacity,
    )
    chat_service = AgentChatService(repository=AgentChatOper())
    task_repository = TransactionalAgentTaskRepository(SessionFactory)
    chat_repository = cast(AgentChatRepositoryFactory, AgentChatOper)
    data = AgentDataContext(
        chat=chat_service,
        chat_persistence=persistence,
        tasks=task_repository,
        users=build_transactional_user_repository(),
        sites=site,
        subscriptions=subscription,
        subscription_mutation_scope=subscription_mutation_scope,
        subscription_delete_scope=delete_subscribe_scope,
        async_rule_group_mutation_scope=partial(
            async_rule_group_mutation_scope,
            system_config.publish_many,
        ),
        subscription_history=subscription_history,
        transfer_history=transfer_history,
        transfer_execution=transfer_execution,
        download_history=download_history,
        plugin_data=TransactionalPluginDataRepository(async_session_scope),
    )
    return AgentComposition(
        data=data,
        chat=AgentChatRuntime(
            async_session=get_async_db,
            repository=chat_repository,
            transaction=SqlAlchemyAsyncUnitOfWork,
            persistence=persistence,
        ),
        tasks=task_repository,
        execution=AgentTaskExecutionService(
            repository=SessionAgentTaskRepository,
            async_executor=runtime.worker,
            sync_transaction=runtime.transaction.sync,
        ),
    )


def publish_agent_services(
    composition: AgentComposition,
    *,
    data_context_registrar: AgentDataContextRegistrar,
) -> None:
    """发布同一批 Agent 服务，并由初始化器登记其数据上下文。"""
    configure_agent_chat_service(composition.data.chat)
    configure_agent_chat_persistence(composition.data.chat_persistence)
    configure_agent_task_execution(composition.execution)
    data_context_registrar(composition.data)
