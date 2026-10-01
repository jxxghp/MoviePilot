"""Agent 数据、会话与自主任务服务的宿主组合根。"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from typing import cast

from app.application.agent import AgentDataContext
from app.application.agenttask import (
    AgentTaskExecutionService,
    configure_agent_task_execution,
    reset_agent_task_execution,
)
from app.application.maintenance import read_cleanup_policy
from app.application.messaging.chat import (
    AgentChatPersistenceService,
    AgentChatService,
    configure_agent_chat_persistence,
    configure_agent_chat_service,
    reset_agent_chat_persistence,
    reset_agent_chat_service,
)
from app.application.messaging.recall import RecallService
from app.db.adapters.agent import (
    SessionAgentTaskRepository,
    TransactionalAgentTaskRepository,
    TransactionalPluginDataRepository,
)
from app.db.adapters.invocation import TransactionalInvocationRepository
from app.db.adapters.recall import LegacyRecallRepository
from app.db.oper.agentchat import AgentChatOper
from app.db.oper.systemconfig import SystemConfigOper
from app.db.session import SessionFactory, async_session_scope
from app.runtime.tasks import TaskRegistry
from app.startup.composition.context import (
    AgentChatRepositoryFactory,
)
from app.startup.composition.database import (
    DatabaseRuntime,
    build_transactional_user_repository,
)
from app.startup.composition.runtime import RuntimeDependencies
from app.startup.composition.subscription import (
    async_rule_group_mutation_scope,
    delete_subscribe_scope,
    subscription_mutation_scope,
)

AgentDataContextRegistrar = Callable[[AgentDataContext], None]
AgentDataContextResetter = Callable[[], None]


@dataclass(frozen=True, slots=True)
class AgentComposition:
    """保存一个 lifespan 内共享的 Agent 数据、会话与任务对象。"""

    data: AgentDataContext
    chat_repository: AgentChatRepositoryFactory
    persistence: AgentChatPersistenceService
    tasks: TransactionalAgentTaskRepository
    execution: AgentTaskExecutionService


def _history_retention_cutoff() -> float | None:
    """独立 Agent 库复用已配置的清理开关和会话保留期，零天表示不自动删除。"""
    policy = read_cleanup_policy()
    return time.time() - policy.agent_chat_days * 86400 if policy.enabled and policy.agent_chat_days else None


def compose_agent(
    *,
    runtime: DatabaseRuntime,
    system_config: SystemConfigOper,
    dependencies: RuntimeDependencies,
    tasks: TaskRegistry | None = None,
) -> AgentComposition:
    """在数据库 worker 启动后构造共享的 Agent 数据与任务服务。"""
    from app.agent.history.storage import SqliteRecallRepository
    from app.agent.runtime import agent_runtime_manager

    persistence = AgentChatPersistenceService(
        repository=AgentChatOper,
        async_executor=runtime.worker,
        sync_transaction=runtime.transaction.sync,
        capacity=runtime.worker.snapshot().capacity,
    )
    recall = RecallService(SqliteRecallRepository(agent_runtime_manager.runtime_dir, legacy=LegacyRecallRepository(SessionFactory), retention_cutoff=_history_retention_cutoff), runtime.worker, tasks)
    chat_service = AgentChatService(repository=AgentChatOper(), recall=recall)
    task_repository = TransactionalAgentTaskRepository(SessionFactory)
    chat_repository = cast(AgentChatRepositoryFactory, AgentChatOper)
    data = AgentDataContext(
        chat=chat_service,
        chat_persistence=persistence,
        tasks=task_repository,
        users=build_transactional_user_repository(),
        sites=dependencies.site,
        subscriptions=dependencies.subscription,
        subscription_mutation_scope=subscription_mutation_scope,
        subscription_delete_scope=delete_subscribe_scope,
        async_rule_group_mutation_scope=partial(
            async_rule_group_mutation_scope,
            system_config.publish_many,
        ),
        subscription_history=dependencies.subscription_history,
        transfer_history=dependencies.transfer_history,
        transfer_execution=dependencies.transfer_execution,
        download_history=dependencies.download_history,
        plugin_data=TransactionalPluginDataRepository(async_session_scope),
        invocations=TransactionalInvocationRepository(SessionFactory),
        recall=recall,
    )
    return AgentComposition(
        data=data,
        chat_repository=chat_repository,
        persistence=persistence,
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


def reset_agent_services(
    *,
    data_context_resetter: AgentDataContextResetter,
) -> None:
    """按发布逆序撤销当前 lifespan 的 Agent 数据、任务与会话服务。"""
    data_context_resetter()
    reset_agent_task_execution()
    reset_agent_chat_persistence()
    reset_agent_chat_service()
