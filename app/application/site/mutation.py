"""站点写操作应用用例。"""

from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Optional, TypeAlias

from app.application.site.contract import (
    SiteMutation,
    SitePriorityMutation,
    SiteStagingPort,
    SiteWriteResult,
)
from app.application.subscription.delete import AsyncUnitOfWork
from app.schemas.common import JsonData

SiteMutationResult: TypeAlias = SiteWriteResult
SiteIndexerLoader = Callable[
    [str],
    Awaitable[Optional[Mapping[str, JsonData]]],
]
SiteEventPublisher = Callable[[dict[str, JsonData]], Awaitable[None]]
DomainExtractor = Callable[[str], str]
UrlNormalizer = Callable[[str], str]


class SiteMutationCommand:
    """统一执行站点新增、更新、优先级和删除事务。"""

    def __init__(
        self,
        *,
        repository: SiteStagingPort,
        unit_of_work: AsyncUnitOfWork,
        auth_level_provider: Callable[[], int],
        indexer_loader: SiteIndexerLoader,
        domain_extractor: DomainExtractor,
        url_normalizer: UrlNormalizer,
        publish_updated: SiteEventPublisher,
        publish_deleted: SiteEventPublisher,
    ) -> None:
        """保存站点验证、持久化、事务和提交后事件端口。"""
        self._repository = repository
        self._unit_of_work = unit_of_work
        self._auth_level_provider = auth_level_provider
        self._indexer_loader = indexer_loader
        self._domain_extractor = domain_extractor
        self._url_normalizer = url_normalizer
        self._publish_updated = publish_updated
        self._publish_deleted = publish_deleted

    async def create(
        self,
        payload: Mapping[str, JsonData],
    ) -> SiteMutationResult:
        """校验并新增站点，提交成功后发布站点更新事件。"""
        values = dict(payload)
        raw_url = values.get("url")
        if not isinstance(raw_url, str) or not raw_url:
            return SiteMutationResult(False, "站点地址不能为空")
        if self._auth_level_provider() < 2:
            return SiteMutationResult(False, "用户未通过认证，无法使用站点功能！")

        domain = self._domain_extractor(raw_url)
        site_info = await self._indexer_loader(domain)
        if not site_info:
            return SiteMutationResult(False, "该站点不支持，请检查站点域名是否正确")
        if await self._repository.async_get_by_domain(domain):
            return SiteMutationResult(False, f"{domain} 站点己存在")

        values.update(
            {
                "domain": domain,
                "url": self._url_normalizer(raw_url),
                "name": site_info.get("name"),
                "public": 1 if site_info.get("public") else 0,
            }
        )
        await self._repository.stage_create(SiteMutation(values))
        await self._commit()
        await self._publish_updated({"domain": domain})
        return SiteMutationResult(True)

    async def update(
        self,
        payload: Mapping[str, JsonData],
    ) -> SiteMutationResult:
        """更新站点并在提交后发布完整的兼容事件载荷。"""
        values = dict(payload)
        site_id = values.get("id")
        if not isinstance(site_id, int) or not site_id or not await self._repository.get_by_id(site_id):
            return SiteMutationResult(False, "站点不存在")

        raw_url = values.get("url")
        normalized_url = self._url_normalizer(raw_url if isinstance(raw_url, str) else "")
        normalized_domain = self._domain_extractor(normalized_url)
        values["url"] = normalized_url
        values["domain"] = normalized_domain
        values.pop("id", None)
        await self._repository.stage_update(site_id, SiteMutation(values))
        await self._commit()
        await self._publish_updated(
            {
                "site_id": site_id,
                "domain": normalized_domain,
                "name": values.get("name"),
                "site_url": normalized_url,
            }
        )
        return SiteMutationResult(True)

    async def update_priorities(
        self,
        priorities: Sequence[Mapping[str, JsonData]],
    ) -> SiteMutationResult:
        """在同一事务中更新全部站点优先级。"""
        mutations: list[SitePriorityMutation] = []
        for priority in priorities:
            site_id = priority.get("id")
            value = priority.get("pri")
            if isinstance(site_id, int) and isinstance(value, int):
                mutations.append(SitePriorityMutation(site_id=site_id, priority=value))
        await self._repository.stage_priorities(tuple(mutations))
        await self._commit()
        return SiteMutationResult(True)

    async def delete(self, site_id: int) -> SiteMutationResult:
        """删除站点，并确保删除事件只在提交成功后发送。"""
        await self._repository.stage_delete(site_id)
        await self._commit()
        await self._publish_deleted({"site_id": site_id})
        return SiteMutationResult(True)

    async def reset(self) -> SiteMutationResult:
        """清空全部站点，并在提交后发布通配站点删除事件。"""
        await self._repository.stage_reset()
        await self._commit()
        await self._publish_deleted({"site_id": "*"})
        return SiteMutationResult(True)

    async def _commit(self) -> None:
        """提交当前站点事务，失败时回滚并保留原始异常。"""
        try:
            await self._unit_of_work.commit()
        except Exception:
            await self._unit_of_work.rollback()
            raise
