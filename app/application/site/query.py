"""站点及站点运行数据的只读应用服务。"""

from __future__ import annotations

import builtins
from typing import Optional

from app.application.site.contract import SiteQueryPort
from app.schemas.site import Site, SiteIconData, SiteStatistic, SiteUserData


class SiteQueryService:
    """把站点持久化快照投影为 API/Chain 可复用的 DTO。"""

    def __init__(self, repository: SiteQueryPort) -> None:
        """保存站点查询仓储端口。"""
        self._repository = repository

    async def list_ordered(self) -> builtins.list[Site]:
        """按站点优先级返回配置 DTO。"""
        return [Site.model_validate(item) for item in await self._repository.async_list_order_by_pri()]

    async def list(self) -> builtins.list[Site]:
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

    async def userdata_latest(self) -> builtins.list[SiteUserData]:
        """返回各站点最新用户数据 DTO。"""
        return [SiteUserData.model_validate(item) for item in await self._repository.async_get_userdata_latest()]

    async def userdata(
        self,
        domain: str,
        workdate: Optional[str] = None,
    ) -> builtins.list[SiteUserData]:
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

    async def statistics(self) -> builtins.list[SiteStatistic]:
        """返回全部站点统计 DTO。"""
        return [SiteStatistic.model_validate(item) for item in await self._repository.async_list_statistics()]

    def userdata_latest_sync(self) -> builtins.list[SiteUserData]:
        """同步返回各站点最新用户数据 DTO。"""
        return [SiteUserData.model_validate(item) for item in self._repository.get_userdata_latest()]


_configured_site_query_service: SiteQueryService | None = None


def configure_site_query_service(service: SiteQueryService) -> None:
    """由启动组合根登记站点查询服务。"""
    global _configured_site_query_service
    _configured_site_query_service = service


def reset_site_query_service() -> None:
    """清除当前 lifespan 的站点查询服务。"""
    global _configured_site_query_service
    _configured_site_query_service = None


def get_configured_site_query_service() -> SiteQueryService:
    """返回启动阶段登记的站点查询服务。"""
    if _configured_site_query_service is None:
        raise RuntimeError("站点查询服务尚未配置")
    return _configured_site_query_service
