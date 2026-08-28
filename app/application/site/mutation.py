"""站点写操作应用用例。"""

import copy
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Optional, TypeAlias

from app.application.configuration import SystemConfigStagingPort
from app.application.outbox import SyncUnitOfWork
from app.application.site.contract import (
    SiteMutation,
    SitePriorityMutation,
    SiteStagingPort,
    SiteWriteResult,
)
from app.application.subscription.contract import (
    SubscriptionPatch,
    SubscriptionReferenceStagingPort,
)
from app.application.subscription.delete import AsyncUnitOfWork
from app.schemas.common import JsonData
from app.schemas.types import SystemConfigKey

SiteMutationResult: TypeAlias = SiteWriteResult
SiteIndexerLoader = Callable[
    [str],
    Awaitable[Optional[Mapping[str, JsonData]]],
]
SiteEventPublisher = Callable[[dict[str, JsonData]], Awaitable[None]]
DomainExtractor = Callable[[str], str]
UrlNormalizer = Callable[[str], str]


@dataclass(frozen=True, slots=True)
class SiteReferenceMutation:
    """站点引用在 SystemConfig 和订阅表中原子提交后的结果。"""

    rss_sites: tuple[int, ...]
    subscription_ids: tuple[int, ...]


class SyncSiteReferenceMutationService:
    """在一个同步 UoW 内原子清理 RSS 和订阅站点引用。"""

    def __init__(
        self,
        configuration: SystemConfigStagingPort,
        subscriptions: SubscriptionReferenceStagingPort,
        unit_of_work: SyncUnitOfWork,
        publish: Callable[[Mapping[SystemConfigKey, JsonData]], None],
    ) -> None:
        """注入共享 Session 的配置、订阅、UoW 与提交后快照发布器。"""
        self._configuration = configuration
        self._subscriptions = subscriptions
        self._unit_of_work = unit_of_work
        self._publish = publish

    def apply(self, site_id: int | str) -> SiteReferenceMutation:
        """清空通配站点或移除指定站点，并在任一步失败时整体回滚。"""
        if site_id != "*" and not isinstance(site_id, int):
            raise ValueError("站点引用清理只接受整数 ID 或通配符 *")
        try:
            result, changes = self._stage(site_id)
            self._unit_of_work.commit()
        except Exception:
            self._unit_of_work.rollback()
            raise
        self._publish(copy.deepcopy(changes))
        return result

    def _stage(
        self,
        site_id: int | str,
    ) -> tuple[SiteReferenceMutation, dict[SystemConfigKey, JsonData]]:
        """在锁定的同步事实源中暂存 RSS 和订阅站点变化。"""
        changes: dict[SystemConfigKey, JsonData] = {}
        original_rss = self._configuration.get_for_update(SystemConfigKey.RssSites)
        rss_sites = [
            int(value)
            for value in original_rss
            if isinstance(value, int) and site_id != "*" and value != site_id
        ] if isinstance(original_rss, list) else []
        if rss_sites != (original_rss or []):
            self._configuration.stage_set(SystemConfigKey.RssSites, rss_sites)
            changes[SystemConfigKey.RssSites] = rss_sites

        subscription_ids = []
        for subscription in self._subscriptions.list_for_reference_rewrite():
            original_sites = list(subscription.sites or [])
            sites = [
                value
                for value in original_sites
                if site_id != "*" and value != site_id
            ]
            if sites == original_sites:
                continue
            self._subscriptions.stage_update(
                subscription.id,
                SubscriptionPatch({"sites": sites}),
            )
            subscription_ids.append(subscription.id)
        return (
            SiteReferenceMutation(tuple(rss_sites), tuple(subscription_ids)),
            changes,
        )


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
