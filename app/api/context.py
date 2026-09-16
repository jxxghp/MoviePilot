"""从 FastAPI AppState 读取类型化宿主能力。"""

from collections.abc import AsyncGenerator, Generator
from typing import cast

from fastapi import Depends, Request

from app.application.classification.runtime import ClassificationRuntime
from app.application.configuration import (
    ApiRuntimeConfig,
    get_api_runtime_config_snapshot,
)
from app.application.messaging.chat import AsyncAgentChatRepository, AsyncUnitOfWork
from app.application.outbox import AsyncOutboxDispatchStore, AsyncOutboxStager
from app.application.subscription.contract import (
    SubscriptionHistoryStagingPort,
    SubscriptionQueryPort,
    SubscriptionStagingPort,
)
from app.runtime.tasks import TaskRegistry, get_task_registry
from app.startup.composition.context import (
    AgentChatRuntime,
    HostRuntime,
    SubscriptionRuntime,
)


def get_host_runtime(request: Request) -> HostRuntime:
    """返回当前 lifespan 挂载的宿主运行时。"""
    runtime = getattr(request.app.state, "host_runtime", None)
    if not isinstance(runtime, HostRuntime):
        raise RuntimeError("HostRuntime 尚未由启动组合根装配")
    return runtime


def get_background_task_registry(
    runtime: HostRuntime = Depends(get_host_runtime),
) -> TaskRegistry:
    """返回当前 lifespan 统一管理的后台任务登记器。"""
    return runtime.tasks


def get_classification_runtime(
    runtime: HostRuntime = Depends(get_host_runtime),
) -> ClassificationRuntime:
    """从完整宿主运行时收窄到媒体分类策略能力。"""
    return runtime.classification


def get_background_task_registry_compat(request: Request) -> TaskRegistry:
    """返回协议兼容端点使用的任务登记器，允许未启动 lifespan 的旧调用回退。"""
    runtime = getattr(request.app.state, "host_runtime", None)
    if isinstance(runtime, HostRuntime):
        return runtime.tasks
    return get_task_registry()


def resolve_background_task_registry(value: object) -> TaskRegistry:
    """兼容直接调用 endpoint 的旧入口，并优先使用注入的任务登记器。"""
    if isinstance(value, TaskRegistry):
        return value
    return get_task_registry()


def get_api_runtime_config(
    runtime: HostRuntime = Depends(get_host_runtime),
) -> ApiRuntimeConfig:
    """为当前请求创建稳定的 API 配置快照。"""
    return runtime.configuration.api()


def get_sync_session(
    runtime: HostRuntime = Depends(get_host_runtime),
) -> Generator[object, None, None]:
    """从 HostRuntime 生成请求独占的同步数据库会话。"""
    yield from runtime.persistence.sync_session()


async def get_async_session(
    runtime: HostRuntime = Depends(get_host_runtime),
) -> AsyncGenerator[object, None]:
    """从 HostRuntime 生成请求独占的异步数据库会话。"""
    async for session in runtime.persistence.async_session():
        yield session


def resolve_api_runtime_config(value: object) -> ApiRuntimeConfig:
    """兼容直接调用 endpoint 的旧入口，并统一返回真实配置快照。"""
    if isinstance(value, ApiRuntimeConfig):
        return value
    return get_api_runtime_config_snapshot()


def get_agent_chat_runtime(
    runtime: HostRuntime = Depends(get_host_runtime),
) -> AgentChatRuntime:
    """从完整宿主运行时收窄到 Agent 会话能力。"""
    return runtime.agent_chat


async def get_agent_chat_session(
    runtime: AgentChatRuntime = Depends(get_agent_chat_runtime),
) -> AsyncGenerator[object, None]:
    """从类型化 Agent 会话运行时生成请求独占会话。"""
    async for session in runtime.async_session():
        yield session


def get_agent_chat_repository(
    session: object = Depends(get_agent_chat_session),
    runtime: AgentChatRuntime = Depends(get_agent_chat_runtime),
) -> AsyncAgentChatRepository:
    """构造绑定当前请求会话的 Agent 会话仓储。"""
    return cast(AsyncAgentChatRepository, runtime.repository(session))


def get_agent_chat_transaction(
    session: object = Depends(get_agent_chat_session),
    runtime: AgentChatRuntime = Depends(get_agent_chat_runtime),
) -> AsyncUnitOfWork:
    """构造绑定当前请求会话的 Agent 会话事务端口。"""
    return cast(AsyncUnitOfWork, runtime.transaction(session))


def get_subscription_runtime(
    runtime: HostRuntime = Depends(get_host_runtime),
) -> SubscriptionRuntime:
    """从完整宿主运行时收窄到订阅写事务能力。"""
    return runtime.subscription


async def get_subscription_session(
    runtime: SubscriptionRuntime = Depends(get_subscription_runtime),
) -> AsyncGenerator[object, None]:
    """从订阅运行时生成请求独占的异步会话。"""
    async for session in runtime.async_session():
        yield session


def get_subscription_repository(
    session: object = Depends(get_subscription_session),
    runtime: SubscriptionRuntime = Depends(get_subscription_runtime),
) -> SubscriptionStagingPort:
    """构造绑定当前请求会话的订阅仓储。"""
    return runtime.repository(session)


def get_sync_subscription_repository(
    session: object = Depends(get_sync_session),
    runtime: SubscriptionRuntime = Depends(get_subscription_runtime),
) -> SubscriptionQueryPort:
    """构造绑定当前请求同步 Session 的订阅查询仓储。"""
    return cast(SubscriptionQueryPort, runtime.repository(session))


def get_subscription_history_repository(
    session: object = Depends(get_subscription_session),
    runtime: SubscriptionRuntime = Depends(get_subscription_runtime),
) -> SubscriptionHistoryStagingPort:
    """构造绑定当前请求会话的订阅历史仓储。"""
    return runtime.history_repository(session)


def get_subscription_transaction(
    session: object = Depends(get_subscription_session),
    runtime: SubscriptionRuntime = Depends(get_subscription_runtime),
) -> AsyncUnitOfWork:
    """构造绑定当前订阅请求会话的异步事务端口。"""
    return cast(AsyncUnitOfWork, runtime.transaction(session))


def get_subscription_outbox(
    session: object = Depends(get_subscription_session),
    runtime: SubscriptionRuntime = Depends(get_subscription_runtime),
) -> AsyncOutboxStager:
    """构造与订阅写入共享请求会话的 outbox 端口。"""
    return runtime.outbox(session)


def get_subscription_outbox_store(
    runtime: SubscriptionRuntime = Depends(get_subscription_runtime),
) -> AsyncOutboxDispatchStore:
    """返回使用独立短事务的订阅 outbox 派发存储。"""
    return runtime.dispatch_store
