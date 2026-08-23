"""Agent 编排和工具使用的数据端口组合根注册表。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


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


class UserPort(_PortProxy):
    """用户数据端口代理。"""

    port_name = "user"


class SitePort(_PortProxy):
    """站点数据端口代理。"""

    port_name = "site"


class SubscribePort(_PortProxy):
    """订阅数据端口代理。"""

    port_name = "subscribe"


class SubscribeHistoryPort(_PortProxy):
    """订阅历史数据端口代理。"""

    port_name = "subscribe_history"


class TransferHistoryPort(_PortProxy):
    """整理历史数据端口代理。"""

    port_name = "transfer_history"


class DownloadHistoryPort(_PortProxy):
    """下载历史数据端口代理。"""

    port_name = "download_history"


class WorkflowPort(_PortProxy):
    """工作流数据端口代理。"""

    port_name = "workflow"


class PluginDataPort(_PortProxy):
    """插件数据端口代理。"""

    port_name = "plugin_data"


class AgentDataPorts:
    """Agent 入口所需的持久化端口集合。"""

    def __init__(self, **factories: AgentDataFactory) -> None:
        """保存各数据能力的工厂。"""
        self.__dict__.update(factories)


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
        "workflow",
        "plugin_data",
    }
    missing = sorted(required - factories.keys())
    if missing:
        raise ValueError(f"Agent 数据端口缺少实现: {', '.join(missing)}")
    global _ports
    _ports = AgentDataPorts(**{name: factories[name] for name in required})


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


def get_agent_user_port() -> Any:
    """创建 Agent 用户数据端口实例。"""
    return get_agent_data_ports().user()


def get_agent_site_port() -> Any:
    """创建 Agent 站点数据端口实例。"""
    return get_agent_data_ports().site()


def get_agent_subscribe_port() -> Any:
    """创建 Agent 订阅数据端口实例。"""
    return get_agent_data_ports().subscribe()


def get_agent_subscribe_history_port() -> Any:
    """创建 Agent 订阅历史数据端口实例。"""
    return get_agent_data_ports().subscribe_history()


def get_agent_transfer_history_port() -> Any:
    """创建 Agent 整理历史数据端口实例。"""
    return get_agent_data_ports().transfer_history()


def get_agent_download_history_port() -> Any:
    """创建 Agent 下载历史数据端口实例。"""
    return get_agent_data_ports().download_history()


def get_agent_workflow_port() -> Any:
    """创建 Agent 工作流数据端口实例。"""
    return get_agent_data_ports().workflow()


def get_agent_plugin_data_port() -> Any:
    """创建 Agent 插件数据端口实例。"""
    return get_agent_data_ports().plugin_data()
