"""站点访问统计写入应用服务。"""

from __future__ import annotations

from typing import Any, Optional, Protocol


class SiteHealthRepository(Protocol):
    """站点健康统计所需的最小写端口。"""

    def success(self, domain: str, seconds: Optional[int] = None) -> Any:
        """记录站点访问成功。"""
        ...

    def fail(self, domain: str) -> Any:
        """记录站点访问失败。"""
        ...

    async def async_success(self, domain: str, seconds: Optional[int] = None) -> Any:
        """异步记录站点访问成功。"""
        ...

    async def async_fail(self, domain: str) -> Any:
        """异步记录站点访问失败。"""
        ...


class SiteHealthService:
    """集中承接索引模块的站点健康统计写操作。"""

    def __init__(self, repository: SiteHealthRepository) -> None:
        """保存站点统计写端口。"""
        self._repository = repository

    def success(self, domain: str, seconds: Optional[int] = None) -> Any:
        """记录同步站点访问成功。"""
        return self._repository.success(domain, seconds)

    def fail(self, domain: str) -> Any:
        """记录同步站点访问失败。"""
        return self._repository.fail(domain)

    async def async_success(self, domain: str, seconds: Optional[int] = None) -> Any:
        """记录异步站点访问成功。"""
        return await self._repository.async_success(domain, seconds)

    async def async_fail(self, domain: str) -> Any:
        """记录异步站点访问失败。"""
        return await self._repository.async_fail(domain)


_configured_site_health_service: SiteHealthService | None = None


def configure_site_health_service(service: SiteHealthService) -> None:
    """由启动组合根登记站点健康统计服务。"""
    global _configured_site_health_service
    _configured_site_health_service = service


def get_configured_site_health_service() -> SiteHealthService:
    """返回启动阶段登记的站点健康统计服务。"""
    if _configured_site_health_service is None:
        raise RuntimeError("站点健康统计服务尚未配置")
    return _configured_site_health_service
