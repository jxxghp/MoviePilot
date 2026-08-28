"""Agent 编排和工具使用的数据端口组合根注册表。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, cast

from app.application.history import (
    DownloadHistoryRepository,
    TransferHistoryRepository,
)
from app.application.security.user import ChainUserRepository
from app.application.site.contract import SiteRepository
from app.application.subscription.contract import (
    SubscriptionHistoryQueryPort,
    SubscriptionRepository,
)

AgentDataFactory = Callable[[], Any]


class _PortMeta(type):
    """支持旧测试按 Oper 方法打桩的端口代理元类。"""

    def __getattr__(cls, name: str) -> Any:
        """把类级方法访问转发到当前配置端口。"""
        return getattr(cls(), name)


class _PortProxy(metaclass=_PortMeta):
    """将存量 Oper 调用形态转发到 Agent 数据端口。"""

    port_name: str

    def __getattr__(self, name: str) -> Any:
        """转发未被测试替换的实例方法。"""
        ports = get_agent_data_ports()
        return getattr(getattr(ports, self.port_name)(), name)


class AgentChatPort(_PortProxy):
    """Agent 会话数据端口代理。"""

    port_name = "agent_chat"


class AgentTaskPort(_PortProxy):
    """Agent 定时任务数据端口代理。"""

    port_name = "agent_task"


class TransferHistoryPort(_PortProxy):
    """整理历史数据端口代理。"""

    port_name = "transfer_history"


class DownloadHistoryPort(_PortProxy):
    """下载历史数据端口代理。"""

    port_name = "download_history"


class PluginDataPort(_PortProxy):
    """插件数据端口代理。"""

    port_name = "plugin_data"


@dataclass(frozen=True, slots=True)
class AgentDataPorts:
    """Agent 入口所需的持久化端口集合。"""

    agent_chat: AgentDataFactory
    agent_task: AgentDataFactory
    user: AgentDataFactory
    site: AgentDataFactory
    subscribe: AgentDataFactory
    subscribe_history: AgentDataFactory
    transfer_history: AgentDataFactory
    download_history: AgentDataFactory
    plugin_data: AgentDataFactory


_ports: AgentDataPorts | None = None


def configure_agent_data_ports(**factories: AgentDataFactory) -> None:
    """由启动组合根登记 Agent 数据端口实现。"""
    required = {
        "agent_chat",
        "agent_task",
        "user",
        "site",
        "subscribe",
        "subscribe_history",
        "transfer_history",
        "download_history",
        "plugin_data",
    }
    missing = sorted(required - factories.keys())
    if missing:
        raise ValueError(f"Agent 数据端口缺少实现: {', '.join(missing)}")
    global _ports
    _ports = AgentDataPorts(
        agent_chat=factories["agent_chat"],
        agent_task=factories["agent_task"],
        user=factories["user"],
        site=factories["site"],
        subscribe=factories["subscribe"],
        subscribe_history=factories["subscribe_history"],
        transfer_history=factories["transfer_history"],
        download_history=factories["download_history"],
        plugin_data=factories["plugin_data"],
    )


def get_agent_data_ports() -> AgentDataPorts:
    """返回已登记的 Agent 数据端口。"""
    if _ports is None:
        raise RuntimeError("Agent 数据端口尚未配置")
    return _ports


def get_agent_chat_port() -> Any:
    """创建 Agent 会话数据端口实例。"""
    return get_agent_data_ports().agent_chat()


def get_agent_task_port() -> Any:
    """创建 Agent 定时任务数据端口实例。"""
    return get_agent_data_ports().agent_task()


def get_agent_user_port() -> ChainUserRepository:
    """创建 Agent 用户数据端口实例。"""
    return cast(ChainUserRepository, get_agent_data_ports().user())


def get_agent_site_port() -> SiteRepository:
    """创建 Agent 类型化站点查询与写入端口实例。"""
    return cast(SiteRepository, get_agent_data_ports().site())


def get_agent_subscribe_port() -> SubscriptionRepository:
    """创建 Agent 类型化订阅查询与写入端口实例。"""
    return cast(SubscriptionRepository, get_agent_data_ports().subscribe())


def get_agent_subscribe_history_port() -> SubscriptionHistoryQueryPort:
    """创建 Agent 类型化订阅历史查询端口实例。"""
    return cast(
        SubscriptionHistoryQueryPort,
        get_agent_data_ports().subscribe_history(),
    )


def get_agent_transfer_history_port() -> TransferHistoryRepository:
    """创建 Agent 类型化整理历史数据端口实例。"""
    return cast(
        TransferHistoryRepository,
        get_agent_data_ports().transfer_history(),
    )


def get_agent_download_history_port() -> DownloadHistoryRepository:
    """创建 Agent 类型化下载历史数据端口实例。"""
    return cast(
        DownloadHistoryRepository,
        get_agent_data_ports().download_history(),
    )


def get_agent_plugin_data_port() -> Any:
    """创建 Agent 插件数据端口实例。"""
    return get_agent_data_ports().plugin_data()
