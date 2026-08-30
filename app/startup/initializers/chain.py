"""Chain 外部技术端口的生命周期入口。"""

from app.startup.composition.chain import (
    configure_chain_port_composition,
    reset_chain_port_composition,
)


def init_chain_ports() -> None:
    """委托组合根装配 Chain 外部服务与系统端口。"""
    configure_chain_port_composition()


def reset_chain_ports() -> None:
    """委托组合根释放 Chain 外部服务与系统端口。"""
    reset_chain_port_composition()
