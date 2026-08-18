"""数据库连通性应用服务。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Optional


DatabaseProbe = Callable[[], Optional[str]]


class DatabaseHealthService:
    """为模块和诊断入口提供不暴露会话实现的数据库探测能力。"""

    def __init__(self, probe: DatabaseProbe) -> None:
        """保存由组合根提供的数据库探测端口。"""
        self._probe = probe

    def test(self) -> Optional[str]:
        """执行数据库探测，成功返回空值，失败返回说明。"""
        return self._probe()


_configured_database_health: DatabaseHealthService | None = None


def configure_database_health(service: DatabaseHealthService) -> None:
    """由启动组合根登记数据库探测服务。"""
    global _configured_database_health
    _configured_database_health = service


def get_configured_database_health() -> DatabaseHealthService:
    """返回启动阶段登记的数据库探测服务。"""
    if _configured_database_health is None:
        raise RuntimeError("数据库探测服务尚未配置")
    return _configured_database_health
