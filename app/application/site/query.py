"""站点及站点运行数据的只读应用服务。"""

from __future__ import annotations

from typing import Any, Optional, Protocol

from app.schemas.site import SiteIconData, SiteStatistic, SiteUserData
from app.schemas.workflow import Site


class SiteQueryRepository(Protocol):
    """站点查询用例需要的最小持久化端口。"""

    async def async_list_order_by_pri(self) -> list[Any]:
        """按优先级读取站点。"""
        ...

    async def async_list(self) -> list[Any]:
        """读取全部站点。"""
        ...

    async def async_get(self, site_id: int) -> Optional[Any]:
        """按 ID 读取站点。"""
        ...

    async def async_get_by_domain(self, domain: str) -> Optional[Any]:
        """按域名读取站点。"""
        ...

    async def async_get_userdata_latest(self) -> list[Any]:
        """读取各站点最新用户数据。"""
        ...

    async def async_get_userdata_by_domain(
        self,
        domain: str,
        workdate: Optional[str] = None,
    ) -> list[Any]:
        """读取站点用户数据。"""
        ...

    async def async_get_icon_by_domain(self, domain: str) -> Optional[Any]:
        """按域名读取站点图标。"""
        ...

    async def async_get_statistic_by_domain(self, domain: str) -> Optional[Any]:
        """按域名读取站点统计。"""
        ...

    async def async_list_statistics(self) -> list[Any]:
        """读取全部站点统计。"""
        ...

    def get(self, site_id: int) -> Optional[Any]:
        """同步按 ID 读取站点。"""
        ...

    def list(self) -> list[Any]:
        """同步读取全部站点。"""
        ...

    def get_userdata_latest(self) -> list[Any]:
        """同步读取各站点最新用户数据。"""
        ...


class SiteQueryService:
    """把站点 ORM 投影为 API/Chain 可复用的稳定 DTO。"""

    def __init__(self, repository: SiteQueryRepository) -> None:
        """保存站点查询仓储端口。"""
        self._repository = repository

    async def list_ordered(self) -> list[Site]:
        """按站点优先级返回配置 DTO。"""
        return [Site.model_validate(item) for item in await self._repository.async_list_order_by_pri()]

    async def list(self) -> list[Site]:
        """返回全部站点配置 DTO。"""
        return [Site.model_validate(item) for item in await self._repository.async_list()]

    async def get(self, site_id: int) -> Optional[Site]:
        """按 ID 返回站点配置 DTO。"""
        item = await self._repository.async_get(site_id)
        return Site.model_validate(item) if item else None

    def get_sync(self, site_id: int) -> Optional[Site]:
        """同步按 ID 返回站点配置 DTO。"""
        item = self._repository.get(site_id)
        return Site.model_validate(item) if item else None

    async def get_by_domain(self, domain: str) -> Optional[Site]:
        """按域名返回站点配置 DTO。"""
        item = await self._repository.async_get_by_domain(domain)
        return Site.model_validate(item) if item else None

    async def userdata_latest(self) -> list[SiteUserData]:
        """返回各站点最新用户数据 DTO。"""
        return [
            SiteUserData.model_validate(item)
            for item in await self._repository.async_get_userdata_latest()
        ]

    async def userdata(
        self,
        domain: str,
        workdate: Optional[str] = None,
    ) -> list[SiteUserData]:
        """返回指定站点用户数据 DTO。"""
        return [
            SiteUserData.model_validate(item)
            for item in await self._repository.async_get_userdata_by_domain(
                domain,
                workdate,
            )
        ]

    async def icon(self, domain: str) -> Optional[SiteIconData]:
        """返回站点图标 DTO。"""
        item = await self._repository.async_get_icon_by_domain(domain)
        if not item:
            return None
        return SiteIconData(
            icon=item.base64 if item.base64 else item.url,
        )

    async def statistic(self, domain: str) -> SiteStatistic:
        """返回指定站点统计 DTO，未命中时返回空统计。"""
        item = await self._repository.async_get_statistic_by_domain(domain)
        return SiteStatistic.model_validate(item) if item else SiteStatistic(domain=domain)

    async def statistics(self) -> list[SiteStatistic]:
        """返回全部站点统计 DTO。"""
        return [
            SiteStatistic.model_validate(item)
            for item in await self._repository.async_list_statistics()
        ]

    def userdata_latest_sync(self) -> list[SiteUserData]:
        """同步返回各站点最新用户数据 DTO。"""
        return [
            SiteUserData.model_validate(item)
            for item in self._repository.get_userdata_latest()
        ]


_configured_site_query_service: SiteQueryService | None = None


def configure_site_query_service(service: SiteQueryService) -> None:
    """由启动组合根登记站点查询服务。"""
    global _configured_site_query_service
    _configured_site_query_service = service


def get_configured_site_query_service() -> SiteQueryService:
    """返回启动阶段登记的站点查询服务。"""
    if _configured_site_query_service is None:
        raise RuntimeError("站点查询服务尚未配置")
    return _configured_site_query_service
