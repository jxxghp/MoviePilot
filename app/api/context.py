"""从 FastAPI AppState 读取类型化宿主能力。"""

from collections.abc import AsyncGenerator, Generator
from typing import cast

from fastapi import Depends, Request

from app.application.messaging.chat import AsyncAgentChatRepository, AsyncUnitOfWork
from app.application.outbox import AsyncOutboxTransaction
from app.application.configuration import (
    ApiRuntimeConfig,
    get_api_runtime_config_snapshot,
)
from app.application.subscription.delete import SubscribeDeletionRepository
from app.application.subscription.identity import SubscribeIdentityDeletionRepository
from app.application.subscription.mutation import (
    SubscriptionHistoryMutationRepository,
    SubscriptionMutationRepository,
)
from app.startup.ports.context import (
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
) -> (
    SubscriptionMutationRepository
    | SubscribeDeletionRepository
    | SubscribeIdentityDeletionRepository
):
    """构造绑定当前请求会话的订阅仓储。"""
    return runtime.repository(session)


def get_subscription_history_repository(
    session: object = Depends(get_subscription_session),
    runtime: SubscriptionRuntime = Depends(get_subscription_runtime),
) -> SubscriptionHistoryMutationRepository:
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
) -> AsyncOutboxTransaction:
    """构造与订阅写入共享请求会话的 outbox 端口。"""
    return runtime.outbox(session)
