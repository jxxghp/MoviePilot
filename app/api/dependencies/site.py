"""站点领域的请求级 command/query 依赖。"""

from typing import Any

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.api.context import get_async_session, get_host_runtime, get_sync_session
from app.application.site.mutation import SiteMutationCommand
from app.application.site.query import SiteQueryService
from app.application.site.sites import SitesHelper  # pylint: disable=import-error,no-name-in-module
from app.domain import site as site_rules
from app.foundation import url as url_tools
from app.runtime.events import eventmanager
from app.schemas.types import EventType
from app.startup.ports.context import HostRuntime


async def _publish_site_updated(payload: dict[str, Any]) -> None:
    """发布已提交的站点更新事件。"""
    await eventmanager.async_send_event(EventType.SiteUpdated, payload)


async def _publish_site_deleted(payload: dict[str, Any]) -> None:
    """发布已提交的站点删除事件。"""
    await eventmanager.async_send_event(EventType.SiteDeleted, payload)


def get_site_mutation_command(
    db: AsyncSession = Depends(get_async_session),
    runtime: HostRuntime = Depends(get_host_runtime),
) -> SiteMutationCommand:
    """组装请求级站点写用例及其事务和外部目录依赖。"""
    sites_helper = SitesHelper()

    def normalize_url(value: str) -> str:
        """沿用站点接口的 scheme/netloc 规范化格式。"""
        scheme, netloc = url_tools.split_netloc(value)
        return f"{scheme}://{netloc}/"

    return SiteMutationCommand(
        repository=runtime.site.repository(db),
        unit_of_work=runtime.persistence.async_transaction(db),
        auth_level_provider=lambda: sites_helper.auth_level,
        indexer_loader=sites_helper.async_get_indexer,
        domain_extractor=site_rules.extract_domain,
        url_normalizer=normalize_url,
        publish_updated=_publish_site_updated,
        publish_deleted=_publish_site_deleted,
    )


def get_site_query_service(
    db: AsyncSession = Depends(get_async_session),
    runtime: HostRuntime = Depends(get_host_runtime),
) -> SiteQueryService:
    """组装站点异步查询服务。"""
    return SiteQueryService(repository=runtime.site.repository(db))


def get_site_sync_query_service(
    db: Session = Depends(get_sync_session),
    runtime: HostRuntime = Depends(get_host_runtime),
) -> SiteQueryService:
    """组装站点同步查询服务，用于同步 Chain 路由。"""
    return SiteQueryService(repository=runtime.site.repository(db))
