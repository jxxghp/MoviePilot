"""领域依赖生命周期入口。"""

from app.startup.composition.domain import compose_domain_dependencies


def configure_domain_dependencies() -> None:
    """按生命周期顺序调用领域组合 owner。"""
    compose_domain_dependencies()
