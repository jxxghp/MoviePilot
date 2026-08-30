"""站点访问端口的生命周期入口。"""

from app.startup.composition.site import (
    configure_site_access_composition,
    reset_site_access_composition,
)


def init_site_access_ports() -> None:
    """委托组合根装配全部站点访问技术端口。"""
    configure_site_access_composition()


def reset_site_access_ports() -> None:
    """委托组合根释放全部站点访问技术端口。"""
    reset_site_access_composition()
