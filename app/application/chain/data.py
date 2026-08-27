"""Chain 所需持久化端口的组合根注册表。

Chain 只依赖本模块声明的工厂，不再直接导入数据库 Oper 或 ORM 模型。
具体适配器由 ``app.startup`` 在进程启动时装配，测试也可以登记隔离替身。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Optional

from app.application.transfer.execution import TransferExecutionRepository
from app.application.transfer.workflow import TransferAdmissionRepository
from app.application.workflow import WorkflowExecutionPort

OperFactory = Callable[[], Any]
WorkflowExecutionPortFactory = Callable[[], WorkflowExecutionPort]
TransferAdmissionRepositoryFactory = Callable[[], TransferAdmissionRepository]
TransferExecutionRepositoryFactory = Callable[[], TransferExecutionRepository]


@dataclass(frozen=True, slots=True)
class ChainDataPorts:
    """跨领域 Chain 使用的最小持久化端口工厂集合。"""

    site: OperFactory
    subscribe: OperFactory
    workflow: WorkflowExecutionPortFactory
    download_history: OperFactory
    transfer_history: OperFactory
    transfer_pending: TransferAdmissionRepositoryFactory
    transfer_execution: TransferExecutionRepositoryFactory
    media_server: OperFactory
    download_failure: OperFactory
    user: OperFactory


class _PortProxyMeta(type):
    """让迁移期的 Oper 名称支持按方法打桩，同时仍转发到组合根端口。"""

    def __getattr__(cls, name: str) -> Any:
        """把类级方法访问转发到一个新的端口实例。"""
        return getattr(cls(), name)


class _ChainDataPortProxy(metaclass=_PortProxyMeta):
    """将旧的 Oper 调用形态转发到 Chain 数据端口的内部代理。"""

    port_name: str

    def __getattr__(self, name: str) -> Any:
        """转发未被测试替换的数据操作。"""
        return getattr(getattr(get_chain_data_ports(), self.port_name)(), name)


class SitePortProxy(_ChainDataPortProxy):
    """站点数据端口代理。"""

    port_name = "site"


class SubscribePortProxy(_ChainDataPortProxy):
    """订阅数据端口代理。"""

    port_name = "subscribe"


class WorkflowPortProxy(_ChainDataPortProxy):
    """工作流数据端口代理。"""

    port_name = "workflow"


class DownloadHistoryPortProxy(_ChainDataPortProxy):
    """下载历史数据端口代理。"""

    port_name = "download_history"


class TransferHistoryPortProxy(_ChainDataPortProxy):
    """整理历史数据端口代理。"""

    port_name = "transfer_history"


class MediaServerPortProxy(_ChainDataPortProxy):
    """媒体服务器数据端口代理。"""

    port_name = "media_server"


class DownloadFailurePortProxy(_ChainDataPortProxy):
    """下载失败数据端口代理。"""

    port_name = "download_failure"


class UserPortProxy(_ChainDataPortProxy):
    """用户数据端口代理。"""

    port_name = "user"


_ports: Optional[ChainDataPorts] = None


def configure_chain_data_ports(
        *,
        site: OperFactory,
        subscribe: OperFactory,
        workflow: WorkflowExecutionPortFactory,
        download_history: OperFactory,
        transfer_history: OperFactory,
        transfer_pending: TransferAdmissionRepositoryFactory,
        transfer_execution: TransferExecutionRepositoryFactory,
        media_server: OperFactory,
        download_failure: OperFactory,
        user: OperFactory,
) -> None:
    """由启动组合根登记显式命名的 Chain 数据端口实现。"""
    global _ports
    _ports = ChainDataPorts(
        site=site,
        subscribe=subscribe,
        workflow=workflow,
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


def get_chain_site_port() -> Any:
    """创建站点数据端口实例。"""
    return get_chain_data_ports().site()


def get_chain_subscribe_port() -> Any:
    """创建订阅数据端口实例。"""
    return get_chain_data_ports().subscribe()


def get_chain_workflow_port() -> WorkflowExecutionPort:
    """返回类型化的工作流执行状态事务端口。"""
    return get_chain_data_ports().workflow()


def get_chain_download_history_port() -> Any:
    """创建下载历史数据端口实例。"""
    return get_chain_data_ports().download_history()


def get_chain_transfer_history_port() -> Any:
    """创建整理历史数据端口实例。"""
    return get_chain_data_ports().transfer_history()


def get_chain_transfer_pending_port() -> TransferAdmissionRepository:
    """创建类型化的整理任务 durable admission 仓储。"""
    return get_chain_data_ports().transfer_pending()


def get_chain_transfer_execution_port() -> TransferExecutionRepository:
    """创建类型化的整理步骤执行与终态结算仓储。"""
    return get_chain_data_ports().transfer_execution()


def get_chain_media_server_port() -> Any:
    """创建媒体服务器数据端口实例。"""
    return get_chain_data_ports().media_server()


def get_chain_download_failure_port() -> Any:
    """创建下载失败数据端口实例。"""
    return get_chain_data_ports().download_failure()


def get_chain_user_port() -> Any:
    """创建用户数据端口实例。"""
    return get_chain_data_ports().user()
