"""Chain 网络端口的生命周期入口。"""

from app.startup.composition.network import (
    configure_chain_network_composition,
    reset_chain_network_composition,
)


def init_chain_network_ports() -> None:
    """委托组合根装配 Chain 同步网络与系统端口。"""
    configure_chain_network_composition()


def reset_chain_network_ports() -> None:
    """委托组合根释放 Chain 同步网络与系统端口。"""
    reset_chain_network_composition()
