"""从 FastAPI AppState 读取类型化宿主能力。"""

from collections.abc import AsyncGenerator
from typing import cast

from fastapi import Depends, Request

from app.application.messaging.chat import AsyncAgentChatRepository, AsyncUnitOfWork
from app.startup.context import AgentChatRuntime, HostRuntime


def get_host_runtime(request: Request) -> HostRuntime:
    """返回当前 lifespan 挂载的宿主运行时。"""
    runtime = getattr(request.app.state, "host_runtime", None)
    if not isinstance(runtime, HostRuntime):
        raise RuntimeError("HostRuntime 尚未由启动组合根装配")
    return runtime


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
