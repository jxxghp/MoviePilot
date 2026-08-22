"""API 请求数据端口注册表。"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable, Generator
from typing import Any


SessionProvider = Callable[[], Generator[Any, None, None]]
AsyncSessionProvider = Callable[[], AsyncGenerator[Any, None]]
RepositoryFactory = Callable[[Any], Any]
StandaloneFactory = Callable[[], Any]
UnitOfWorkFactory = Callable[[Any], Any]


class ApiDataPorts:
    """保存 API 依赖所需的会话、仓储和事务端口。"""

    def __init__(
        self,
        *,
        sync_session: SessionProvider,
        async_session: AsyncSessionProvider,
        repositories: dict[str, RepositoryFactory],
        standalone: dict[str, StandaloneFactory],
        unit_of_work: dict[str, UnitOfWorkFactory],
    ) -> None:
        """保存由启动组合根提供的具体实现工厂。"""
        self.sync_session = sync_session
        self.async_session = async_session
        self.repositories = repositories
        self.standalone = standalone
        self.unit_of_work = unit_of_work

    def repository(self, name: str, session: Any) -> Any:
        """按能力名构造请求级仓储。"""
        return self.repositories[name](session)

    def standalone_repository(self, name: str) -> Any:
        """构造不绑定请求会话的持久化端口。"""
        return self.standalone[name]()

    def transaction(self, name: str, session: Any) -> Any:
        """构造请求级事务端口。"""
        return self.unit_of_work[name](session)


_ports: ApiDataPorts | None = None


def configure_api_data_runtime(ports: ApiDataPorts) -> None:
    """让旧全局 Facade 委托启动组合根创建的同一个端口实例。"""
    global _ports
    _ports = ports


def configure_api_data_ports(
    *,
    sync_session: SessionProvider,
    async_session: AsyncSessionProvider,
    repositories: dict[str, RepositoryFactory],
    standalone: dict[str, StandaloneFactory],
    unit_of_work: dict[str, UnitOfWorkFactory],
) -> None:
    """由启动组合根登记 API 数据实现，切断 API 对数据库实现包的直接导入。"""
    configure_api_data_runtime(ApiDataPorts(
        sync_session=sync_session,
        async_session=async_session,
        repositories=repositories,
        standalone=standalone,
        unit_of_work=unit_of_work,
    ))


def get_api_data_ports() -> ApiDataPorts:
    """返回当前 API 数据端口集合。"""
    if _ports is None:
        raise RuntimeError("API 数据端口尚未由启动组合根配置")
    return _ports


def get_db() -> Generator[Any, None, None]:
    """向 FastAPI 暴露同步请求会话依赖。"""
    yield from get_api_data_ports().sync_session()


async def get_async_db() -> AsyncGenerator[Any, None]:
    """向 FastAPI 暴露异步请求会话依赖。"""
    async for session in get_api_data_ports().async_session():
        yield session
