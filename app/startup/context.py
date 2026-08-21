"""宿主启动阶段构建的类型化运行时上下文。"""

from collections.abc import AsyncGenerator, Generator
from dataclasses import dataclass
from typing import Protocol

from app.application.messaging.chat import (
    AsyncAgentChatRepository,
    AsyncUnitOfWork,
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


class CompatibilityApiData(Protocol):
    """未迁移 API 领域继续使用的结构化兼容 Facade。"""

    sync_session: SyncSessionProvider
    async_session: AsyncSessionProvider

    def repository(self, name: str, session: object) -> object:
        """按旧能力名构造请求级仓储。"""
        ...

    def standalone_repository(self, name: str) -> object:
        """按旧能力名构造独立仓储。"""
        ...

    def transaction(self, name: str, session: object) -> object:
        """按旧能力名构造事务端口。"""
        ...


@dataclass(frozen=True, slots=True)
class AgentChatRuntime:
    """Agent 会话 API 可见的最小数据运行时。"""

    async_session: AsyncSessionProvider
    repository: AgentChatRepositoryFactory
    transaction: AsyncUnitOfWorkFactory


@dataclass(frozen=True, slots=True)
class HostRuntime:
    """宿主组合根构建且在一个 FastAPI lifespan 内共享的运行时对象。"""

    agent_chat: AgentChatRuntime
    compatibility_api_data: CompatibilityApiData
