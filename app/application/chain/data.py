"""Chain 所需持久化端口的组合根注册表。

Chain 只依赖本模块声明的工厂，不再直接导入数据库 Oper 或 ORM 模型。
具体适配器由 ``app.startup`` 在进程启动时装配，测试也可以登记隔离替身。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional, Protocol

if TYPE_CHECKING:
    from app.application.download.failures import DownloadFailureRepository
    from app.application.history import (
        DownloadHistoryRepository,
        TransferHistoryRepository,
    )
    from app.application.mediaserver import MediaServerRepository
    from app.application.security.user import ChainUserRepository
    from app.application.site.contract import SiteRepository
    from app.application.subscription.contract import SubscriptionRepository
    from app.application.transfer.execution import TransferExecutionRepository
    from app.application.transfer.workflow import TransferAdmissionRepository

OperFactory = Callable[[], Any]
SiteRepositoryFactory = Callable[[], "SiteRepository"]
SubscriptionRepositoryFactory = Callable[[], "SubscriptionRepository"]
DownloadFailureRepositoryFactory = Callable[[], "DownloadFailureRepository"]
MediaServerRepositoryFactory = Callable[[], "MediaServerRepository"]
ChainUserRepositoryFactory = Callable[[], "ChainUserRepository"]
TransferAdmissionRepositoryFactory = Callable[[], "TransferAdmissionRepository"]
TransferExecutionRepositoryFactory = Callable[[], "TransferExecutionRepository"]


class DownloadHistoryRepositoryFactory(Protocol):
    """构造下载历史仓储且不在注册表导入时初始化其实现。"""

    def __call__(self) -> "DownloadHistoryRepository":
        """返回下载历史仓储。"""
        ...


class TransferHistoryRepositoryFactory(Protocol):
    """构造整理历史仓储且不在注册表导入时初始化其实现。"""

    def __call__(self) -> "TransferHistoryRepository":
        """返回整理历史仓储。"""
        ...


@dataclass(frozen=True, slots=True)
class ChainDataPorts:
    """跨领域 Chain 使用的最小持久化端口工厂集合。"""

    site: SiteRepositoryFactory
    subscribe: SubscriptionRepositoryFactory
    download_history: DownloadHistoryRepositoryFactory
    transfer_history: TransferHistoryRepositoryFactory
    transfer_pending: TransferAdmissionRepositoryFactory
    transfer_execution: TransferExecutionRepositoryFactory
    media_server: MediaServerRepositoryFactory
    download_failure: DownloadFailureRepositoryFactory
    user: ChainUserRepositoryFactory


_ports: Optional[ChainDataPorts] = None


def configure_chain_data_ports(
        *,
        site: SiteRepositoryFactory,
        subscribe: SubscriptionRepositoryFactory,
        download_history: DownloadHistoryRepositoryFactory,
        transfer_history: TransferHistoryRepositoryFactory,
        transfer_pending: TransferAdmissionRepositoryFactory,
        transfer_execution: TransferExecutionRepositoryFactory,
        media_server: MediaServerRepositoryFactory,
        download_failure: DownloadFailureRepositoryFactory,
        user: ChainUserRepositoryFactory,
) -> None:
    """由启动组合根登记显式命名的 Chain 数据端口实现。"""
    global _ports
    _ports = ChainDataPorts(
        site=site,
        subscribe=subscribe,
        download_history=download_history,
        transfer_history=transfer_history,
        transfer_pending=transfer_pending,
        transfer_execution=transfer_execution,
        media_server=media_server,
        download_failure=download_failure,
        user=user,
    )


def get_chain_data_ports() -> ChainDataPorts:
    """返回启动阶段登记的 Chain 数据端口。"""
    if _ports is None:
        raise RuntimeError("Chain 数据端口尚未配置")
    return _ports


def get_chain_site_port() -> SiteRepository:
    """创建类型化站点查询与写入端口实例。"""
    return get_chain_data_ports().site()


def get_chain_subscribe_port() -> SubscriptionRepository:
    """创建类型化订阅查询与写入端口实例。"""
    return get_chain_data_ports().subscribe()


def get_chain_download_history_port() -> DownloadHistoryRepository:
    """创建类型化的下载历史查询与事务端口实例。"""
    return get_chain_data_ports().download_history()


def get_chain_transfer_history_port() -> TransferHistoryRepository:
    """创建类型化的整理历史查询与变更端口实例。"""
    return get_chain_data_ports().transfer_history()


def get_chain_transfer_pending_port() -> TransferAdmissionRepository:
    """创建类型化的整理任务 durable admission 仓储。"""
    return get_chain_data_ports().transfer_pending()


def get_chain_transfer_execution_port() -> TransferExecutionRepository:
    """创建类型化的整理步骤执行与终态结算仓储。"""
    return get_chain_data_ports().transfer_execution()


def get_chain_media_server_port() -> MediaServerRepository:
    """创建类型化的媒体服务器本地缓存端口实例。"""
    return get_chain_data_ports().media_server()


def get_chain_download_failure_port() -> DownloadFailureRepository:
    """创建类型化的下载失败冷却持久化端口实例。"""
    return get_chain_data_ports().download_failure()


def get_chain_user_port() -> ChainUserRepository:
    """创建用户数据端口实例。"""
    return get_chain_data_ports().user()
