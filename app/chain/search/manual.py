"""
原搜索接口的单页请求：只返回页事实，进度与重试由客户端拥有。
"""

import asyncio
import base64
import binascii
import json
from collections import Counter
from contextlib import aclosing
from functools import partial
from typing import TYPE_CHECKING, Any, AsyncGenerator, AsyncIterator, Callable, Literal, Optional, TypeVar, cast
from uuid import uuid4

from app.application.configuration import get_configured_system_config
from app.application.rules import RuleHelper
from app.application.search.state import SearchStateService
from app.application.site.observation import SiteSearchObservation
from app.chain.media import MediaChain
from app.chain.search.contract import _SearchOwnerBase
from app.chain.search.execution import MediaSearchPlan, _candidate_contexts
from app.chain.search.media import _build_missing_media_map
from app.chain.search.result import SearchResultOwner
from app.chain.search.scan import RESTART_AFTER_SECONDS, SearchSources
from app.chain.search.title import SearchTitleOwner, _TitleSearchResolveRequest
from app.domain.context import Context, MediaInfo, TorrentInfo
from app.domain.search import MAX_SEARCH_PAGES, search_page_signature, search_resource_id
from app.runtime.cache import Cache
from app.runtime.execution import await_task_to_terminal
from app.runtime.tasks import get_task_registry
from app.schemas.types import MediaType, SystemConfigKey

if TYPE_CHECKING:
    from app.schemas.search import SearchDiagnostics, SearchDiagnosticSite

_PageResult = TypeVar("_PageResult")
_PAGE_CACHE_REGION = "search.manual.pages"
# 混合来源状态保留最需要用户注意的事实，不能把局部失败覆盖为空结果。
_OUTCOME_PRIORITY = {"empty": 0, "success": 1, "unknown": 2, "cooldown": 3, "error": 4}


def _site_diagnostic(site_id: str, site_name: Optional[str], observation: SiteSearchObservation,
                     returned: int) -> "SearchDiagnosticSite":
    """
    仅投影可信的请求结果与数量，不复制可能含凭据的原始错误。
    """
    from app.schemas.search import SearchDiagnosticSite

    raw_count = observation.raw_count if observation.raw_count is not None else returned
    outcome: Literal["success", "empty", "error", "cooldown", "unknown"] = "unknown"
    if observation.outcome == "deferred":
        outcome = "cooldown"
    elif observation.outcome == "error":
        outcome = "error"
    elif observation.attempted and observation.outcome == "success":
        outcome = "success" if raw_count else "empty"
    return SearchDiagnosticSite(
        site_id=int(site_id) if site_id.isdigit() else None,
        site_name=(site_name or "未知站点") if site_id != "plugin" else "插件资源源",
        attempted=observation.attempted, outcome=outcome, raw_count=max(0, raw_count),
    )


def encode_source(site: str, keyword: str, search_id: Optional[str] = None) -> str:
    """
    来源标识携带本轮搜索编号，隔离不同搜索的页摘要；客户端只需原样回传。
    """
    return base64.urlsafe_b64encode(json.dumps(
        [site, keyword, search_id or uuid4().hex], ensure_ascii=False).encode()).decode().rstrip("=")


def decode_source(source: str) -> tuple[str, str, str]:
    """
    还原来源标识；格式不正确时抛出 ValueError。
    """
    try:
        site, keyword, search_id = json.loads(base64.urlsafe_b64decode(source + "=" * (-len(source) % 4)))
    except (binascii.Error, ValueError, TypeError) as error:
        raise ValueError("搜索来源无效，请重新搜索") from error
    if (not isinstance(site, str) or not isinstance(keyword, str) or not keyword
            or not isinstance(search_id, str) or not search_id):
        raise ValueError("搜索来源无效，请重新搜索")
    return site, keyword, search_id


async def _offload(operation: Callable[..., _PageResult], *args: Any, **kwargs: Any) -> _PageResult:
    """
    托管有限页任务，取消时等 worker 收尾，避免遗留请求。
    """
    task = get_task_registry().create_sync(partial(operation, *args, **kwargs), owner="search.manual")
    try:
        return cast(_PageResult, await asyncio.shield(task))
    except asyncio.CancelledError:
        await await_task_to_terminal(task)
        raise


class _ManualPage:
    """
    一次单页请求只收集原始资源，结果处理委托给原搜索业务。
    """

    def __init__(self, owner: _SearchOwnerBase, plan: MediaSearchPlan, raw_title: bool, search_id: str) -> None:
        """
        持有本轮搜索计划和页摘要，候选只由事件编排层顺序收集。
        """
        self.owner, self.plan, self.raw_title = owner, plan, raw_title
        self.media = cast(MediaInfo, plan.mediainfo)
        self.sources = SearchSources(owner, plan)
        self.torrents: list[TorrentInfo] = []
        self.search_id = search_id
        self.page_cache = Cache(ttl=RESTART_AFTER_SECONDS)
        self.counts: Counter[str] = Counter()
        self.observations: list["SearchDiagnosticSite"] = []

    def read(self, key: str, page: int) -> tuple[dict[str, Any], list[TorrentInfo], list["SearchDiagnosticSite"]]:
        """
        上一页摘要保留 36 小时；失效时先补上一页再取目标页，只有目标页交给原结果处理。
        """
        query = self.sources.queries[key]
        pageable = self.sources.pageable(key)
        source = encode_source(query["site"], query["keyword"], self.search_id)
        facts: dict[str, Any] = {"source": source,
                                "site_name": self.sources.sites.get(query["site"], {}).get("name"),
                                "page": page, "can_continue": False, "error": None}
        observations: list["SearchDiagnosticSite"] = []
        # 超过最大页数，或不支持分页的站点请求后续页时，直接按末页返回。
        if page >= MAX_SEARCH_PAGES or (page > 0 and not pageable):
            return facts, [], observations
        previous = self.page_cache.get(f"{source}:{page - 1}", region=_PAGE_CACHE_REGION) if page else None
        pages = [page - 1, page] if page and previous is None else [page]
        for requested_page in pages:
            # 两次请求均经过原站点流控；失败不写摘要，也不推进客户端指定的页号。
            result = self.sources.fetch(key, requested_page)
            observation = result.observation
            observations.append(_site_diagnostic(query["site"], facts["site_name"], observation, len(result.torrents)))
            if not (observation.attempted and observation.outcome == "success"):
                facts.update(error=observation.error or "本页请求未完成，请继续加载重试",
                             can_continue=query["site"] != "plugin")
                return facts, [], observations
            signature = search_page_signature(search_resource_id(item) for item in result.torrents) or ""
            self.page_cache.set(f"{source}:{requested_page}", signature, region=_PAGE_CACHE_REGION)
            if requested_page < page:
                previous = signature
        raw_count = observation.raw_count if observation.raw_count is not None else len(result.torrents)
        if previous and signature == previous:
            return facts, [], observations
        # 尚未确认末页时允许尝试下一页，不预取，也不保证下一页一定有结果。
        facts.update(can_continue=bool(
            pageable and raw_count and observation.has_more is not False and page + 1 < MAX_SEARCH_PAGES))
        return facts, result.torrents, observations

    def results(self, torrents: Optional[list[TorrentInfo]] = None) -> list[Context]:
        """
        复用原搜索的识别、季集过滤和排序；显示过滤与分页能力无关。
        """
        torrents = self.torrents if torrents is None else torrents
        self.counts.clear()
        if self.raw_title:
            contexts = SearchTitleOwner._resolve_title_request(cast(SearchTitleOwner, self.owner), _TitleSearchResolveRequest(
                title=self.media.title or "", torrents=torrents, rule_groups=self.plan.rule_groups,
                mtype=None if self.media.type == MediaType.UNKNOWN else self.media.type,
            )).contexts
            self.counts["filter_rules"] = len(torrents) - len(contexts)
            return contexts
        return SearchResultOwner._parse_result(
            cast(SearchResultOwner, self.owner), torrents=torrents, mediainfo=self.media, keyword=self.plan.keyword,
            season_episodes=self.sources.season_episodes, rule_groups=self.plan.rule_groups,
            custom_words=self.plan.custom_words, filter_params=self.plan.filter_params,
            diagnostics=self.counts,
        )

    def diagnostics(self, contexts: list[Context]) -> "SearchDiagnostics":
        """
        在完整页处理后生成请求独有的诊断，规则跳过不会伪装成资源拒绝。

        :param contexts: 本页最终过滤结果
        :return: 仅描述本次页请求的安全诊断快照
        """
        from app.schemas.search import (
            SearchDiagnosticCounts,
            SearchDiagnosticRejection,
            SearchDiagnostics,
            SearchDiagnosticWarning,
        )

        helper = RuleHelper()
        media = None if self.raw_title else self.media
        group_names = self.plan.rule_groups or []
        rules = helper.explain_rule_groups(media=media, group_names=group_names)
        warnings = [SearchDiagnosticWarning(code=rule.reason, rule_group=rule.name) for rule in rules if rule.reason]
        warnings.extend(helper.diagnose_rule_groups(media=media, group_names=group_names))
        sites: dict[Optional[int], SearchDiagnosticSite] = {}
        warning_codes: set[str] = set()
        for observation in self.observations:
            if observation.outcome in {"error", "cooldown", "unknown"}:
                warning_codes.add("provider_outcome_unknown" if observation.outcome == "unknown" else f"provider_{observation.outcome}")
            if observation.site_id not in sites:
                sites[observation.site_id] = observation.model_copy(deep=True)
                continue
            site = sites[observation.site_id]
            site.raw_count += observation.raw_count
            site.attempted = site.attempted or observation.attempted
            if _OUTCOME_PRIORITY[observation.outcome] > _OUTCOME_PRIORITY[site.outcome]:
                site.outcome = observation.outcome
        warnings.extend(SearchDiagnosticWarning(code=code) for code in sorted(warning_codes))
        return SearchDiagnostics(
            search_id=self.search_id, scope="page", searched_sites=list(sites.values()),
            stage_counts=SearchDiagnosticCounts(candidates=len(self.torrents), retained=len(contexts)),
            rejection_reasons=[SearchDiagnosticRejection(code=code, count=count)
                               for code, count in sorted(self.counts.items()) if count and code != "matched"],
            rule_groups=rules, warnings=warnings,
        )

    def preview(self, torrents: list[TorrentInfo]) -> list[Context]:
        """
        标题预览复用过滤，精确搜索先显示未匹配候选，最终整页统一校验。
        """
        return self.results(torrents) if self.raw_title else _candidate_contexts(self.media, torrents)


async def _read_pages(
    manual: _ManualPage, keys: list[str], page: int,
) -> AsyncGenerator[tuple[str, dict[str, Any], list[TorrentInfo], list["SearchDiagnosticSite"]], None]:
    """
    复用有界并发和取消收口；同站点多个关键词按轮次串行，先完成的页立即交付。
    """
    grouped: dict[str, list[str]] = {}
    for key in keys:
        grouped.setdefault(manual.sources.queries[key]["site"], []).append(key)

    async def read_source(site: dict[str, Any], requested_page: int) -> list[Any]:
        """
        把指定来源的页事实适配到既有单页调度器。
        """
        return [await _offload(manual.read, site["key"], requested_page)]

    for index in range(max(map(len, grouped.values()), default=0)):
        pages = manual.owner._iter_site_page_results(
            indexer_sites=[{"key": group[index]} for group in grouped.values() if index < len(group)],
            search_pages=[page], search_page=read_source, should_continue=lambda _site, _items: False,
            task_owner="chain.search.manual.site_page",
        )
        async with aclosing(pages):
            async for site, _page, results, _continued, _elapsed in pages:
                facts, torrents, observations = results[0]
                yield site["key"], facts, torrents, observations


async def _prepare_page(owner: _SearchOwnerBase, params: dict[str, Any]) -> _ManualPage:
    """
    只构造本次请求的识别/关键词计划，不读写任何搜索检查点。
    """
    source, media_id = params.get("media_source"), params.get("media_id")
    raw_title = not (source and media_id)
    if raw_title:
        keyword = params["keyword"]
        media = MediaInfo(title=keyword, names=[keyword], type=params.get("mtype") or MediaType.UNKNOWN)
    else:
        recognized = await MediaChain().async_recognize_media(media_source=source, media_id=media_id, mtype=params.get("mtype"))
        if not isinstance(recognized, MediaInfo):
            raise ValueError("媒体信息识别失败")
        supplemented = await MediaChain().async_supplement_media_info(mediainfo=recognized)
        media = supplemented if isinstance(supplemented, MediaInfo) else recognized
    rules = get_configured_system_config().get(SystemConfigKey.SearchFilterRuleGroups) or []
    sites, keyword = params.get("sites"), params.get("keyword") if raw_title else None
    search_id = uuid4().hex
    if continuation := params.get("source"):
        site, keyword, search_id = decode_source(continuation)
        sites = [int(site)] if site.isdigit() else []
    plan = MediaSearchPlan(media, keyword=keyword, sites=sites,
                           area=params.get("area") or "title", rule_groups=rules,
                           no_exists=None if raw_title else _build_missing_media_map(media, params.get("season")))
    return await _offload(_ManualPage, owner, plan, raw_title, search_id)


async def _page_events(manual: _ManualPage, params: dict[str, Any]) -> AsyncGenerator[dict[str, Any], None]:
    """
    每个来源只返回指定页；只有首页请求才使用别名回退和插件来源。
    """
    page = max(0, int(params.get("page") or 0))
    initial = not params.get("source")
    if not initial:
        # 续页沿用来源标识里的实际关键词，不再经站点关键词转换，避免换成另一个来源。
        site, keyword, _search_id = decode_source(params["source"])
        manual.sources.queries = {f"{site}:{keyword}": {"site": site, "keyword": keyword}} if site in manual.sources.sites else {}
        keys = list(manual.sources.queries)
    else:
        keys = manual.sources.add_keywords(all_names=bool(manual.owner.runtime_config.search_multiple_name))
    facts_by_key: dict[str, dict[str, Any]] = {}
    torrents_by_key: dict[str, list[TorrentInfo]] = {}
    contexts: list[Context] = []
    preview_count = 0
    while keys:
        pages = _read_pages(manual, keys, page)
        async with aclosing(pages):
            async for key, facts, torrents, observations in pages:
                facts_by_key[key], torrents_by_key[key] = facts, torrents
                manual.observations.extend(observations)
                manual.torrents.extend(torrents)
                previews = await _offload(manual.preview, torrents)
                preview_count += len(previews)
                yield {"type": "append", "stage": "searching",
                       "value": len(facts_by_key) / len(manual.sources.queries) * 90,
                       "text": f"已完成 {len(facts_by_key)} / {len(manual.sources.queries)} 个搜索来源",
                       "items": [context.to_dict() for context in previews],
                       "total_items": preview_count, "candidate_items": len(manual.torrents)}
        # 并发完成顺序只影响预览，完整结果仍保持原来源顺序和最终排序语义。
        manual.torrents = [torrent for key in manual.sources.queries for torrent in torrents_by_key.get(key, [])]
        yield {"type": "progress", "stage": "filtering", "value": 98,
               "text": f"正在过滤匹配 {len(manual.torrents)} 个候选资源 ..."}
        contexts = await _offload(manual.results)
        if contexts or not initial:
            break
        keys = manual.sources.add_keywords()
    diagnostics = await _offload(manual.diagnostics, contexts)
    if initial:
        # 首页沿用原搜索的结果缓存与上次搜索参数；续页不覆盖它们，累计结果由客户端持有。
        await manual.owner._async_save_results(contexts)
        cache_params: dict[str, Any] = dict(
            keyword=params.get("keyword") if manual.raw_title else None,
            media_source=None if manual.raw_title else manual.media.media_source,
            media_id=None if manual.raw_title else manual.media.media_id, mtype=manual.media.type,
            area=manual.plan.area, season=None if manual.raw_title else params.get("season"),
            sites=params.get("sites"), result_type="torrent")
        manual.owner.save_last_search_params(**cache_params)
        if isinstance(params.get("user_id"), int):
            from app.schemas.search import SearchLastContextData

            await manual.owner.async_save_search_context(params["user_id"], SearchLastContextData.model_validate({
                "params": SearchStateService.build_params(**cache_params) or {},
                "results": [context.to_dict() for context in contexts], "diagnostics": diagnostics,
            }))
    items = [context.to_dict() for context in contexts]
    source_facts = [facts_by_key[key] for key in manual.sources.queries if key in facts_by_key]
    yield {"type": "replace", "stage": "filtered", "value": 100, "items": items,
           "total_items": len(items), "candidate_items": len(manual.torrents), "sources": source_facts}
    yield {"type": "done", "stage": "done", "total_items": len(items), "sources": source_facts,
           "diagnostics": diagnostics.model_dump(mode="json")}


class SearchManualOwner:
    """
    Web 单页结果与后端页摘要缓存，沿用既有站点、别名和媒体识别策略。
    """

    @staticmethod
    async def events(owner: _SearchOwnerBase, *, params: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        """
        原 API 显式单页模式；首次请求各来源第 0 页，续页按来源标识请求指定页。
        """
        try:
            manual = await _prepare_page(owner, params)
        except ValueError as error:
            yield {"type": "error", "message": str(error)}
            return
        async with aclosing(_page_events(manual, params)) as events:
            async for event in events:
                yield event
