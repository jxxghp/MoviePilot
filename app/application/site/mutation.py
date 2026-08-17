"""站点写操作应用用例。"""

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping, Optional, Protocol

from app.application.subscription.delete import AsyncUnitOfWork


@dataclass(frozen=True, slots=True)
class SiteMutationResult:
    """描述站点写操作是否成功及兼容提示信息。"""

    success: bool
    message: str = ""


class SiteMutationRepository(Protocol):
    """站点写用例需要的最小持久化端口。"""

    async def get_by_id(self, site_id: int) -> Optional[Any]:
        """读取指定站点。"""
        ...

    async def get_by_domain(self, domain: str) -> Optional[Any]:
        """按域名读取站点。"""
        ...

    async def stage_create(self, payload: Mapping[str, Any]) -> None:
        """暂存新增站点。"""
        ...

    async def stage_update(self, site_id: int, payload: Mapping[str, Any]) -> bool:
        """暂存站点更新并返回目标是否存在。"""
        ...

    async def stage_delete(self, site_id: int) -> None:
        """暂存站点删除。"""
        ...

    async def stage_priorities(self, priorities: list[dict]) -> None:
        """暂存一组站点优先级变更。"""
        ...


SiteIndexerLoader = Callable[[str], Awaitable[Optional[dict]]]
SiteEventPublisher = Callable[[dict], Awaitable[None]]
DomainExtractor = Callable[[str], str]
UrlNormalizer = Callable[[str], str]


class SiteMutationCommand:
    """统一执行站点新增、更新、优先级和删除事务。"""

    def __init__(
            self,
            *,
            repository: SiteMutationRepository,
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

    async def create(self, payload: Mapping[str, Any]) -> SiteMutationResult:
        """校验并新增站点，提交成功后发布站点更新事件。"""
        values = dict(payload)
        raw_url = values.get("url")
        if not raw_url:
            return SiteMutationResult(False, "站点地址不能为空")
        if self._auth_level_provider() < 2:
            return SiteMutationResult(False, "用户未通过认证，无法使用站点功能！")

        domain = self._domain_extractor(raw_url)
        site_info = await self._indexer_loader(domain)
        if not site_info:
            return SiteMutationResult(False, "该站点不支持，请检查站点域名是否正确")
        if await self._repository.get_by_domain(domain):
            return SiteMutationResult(False, f"{domain} 站点己存在")

        values.update({
            "id": None,
            "domain": domain,
            "url": self._url_normalizer(raw_url),
            "name": site_info.get("name"),
            "public": 1 if site_info.get("public") else 0,
        })
        await self._repository.stage_create(values)
        await self._commit()
        await self._publish_updated({"domain": domain})
        return SiteMutationResult(True)

    async def update(self, payload: Mapping[str, Any]) -> SiteMutationResult:
        """更新站点并在提交后发布完整的兼容事件载荷。"""
        values = dict(payload)
        site_id = values.get("id")
        if not site_id or not await self._repository.get_by_id(site_id):
            return SiteMutationResult(False, "站点不存在")

        values["url"] = self._url_normalizer(values.get("url") or "")
        values["domain"] = self._domain_extractor(values["url"])
        await self._repository.stage_update(site_id, values)
        await self._commit()
        await self._publish_updated({
            "site_id": site_id,
            "domain": values["domain"],
            "name": values.get("name"),
            "site_url": values["url"],
        })
        return SiteMutationResult(True)

    async def update_priorities(self, priorities: list[dict]) -> SiteMutationResult:
        """在同一事务中更新全部站点优先级。"""
        await self._repository.stage_priorities(priorities)
        await self._commit()
        return SiteMutationResult(True)

    async def delete(self, site_id: int) -> SiteMutationResult:
        """删除站点，并确保删除事件只在提交成功后发送。"""
        await self._repository.stage_delete(site_id)
        await self._commit()
        await self._publish_deleted({"site_id": site_id})
        return SiteMutationResult(True)

    async def _commit(self) -> None:
        """提交当前站点事务，失败时回滚并保留原始异常。"""
        try:
            await self._unit_of_work.commit()
        except Exception:
            await self._unit_of_work.rollback()
            raise
