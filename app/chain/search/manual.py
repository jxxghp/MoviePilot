"""原搜索接口的单页请求：只返回页事实，进度与重试由客户端拥有。"""

import asyncio
import base64
import binascii
import json
from functools import partial
from typing import Any, AsyncIterator, Callable, Optional, TypeVar, cast
from uuid import uuid4

from app.application.configuration import get_configured_system_config
from app.chain.media import MediaChain
from app.chain.search.contract import _SearchOwnerBase
from app.chain.search.execution import MediaSearchPlan
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

_PageResult = TypeVar("_PageResult")
_PAGE_CACHE_REGION = "search.manual.pages"


def encode_source(site: str, keyword: str, search_id: Optional[str] = None) -> str:
    """来源标识携带本轮搜索编号，隔离不同搜索的页摘要；客户端只需原样回传。"""
    return base64.urlsafe_b64encode(json.dumps(
        [site, keyword, search_id or uuid4().hex], ensure_ascii=False).encode()).decode().rstrip("=")


def decode_source(source: str) -> tuple[str, str, str]:
    """还原来源标识；格式不正确时抛出 ValueError。"""
    try:
        site, keyword, search_id = json.loads(base64.urlsafe_b64decode(source + "=" * (-len(source) % 4)))
    except (binascii.Error, ValueError, TypeError) as error:
        raise ValueError("搜索来源无效，请重新搜索") from error
    if (not isinstance(site, str) or not isinstance(keyword, str) or not keyword
            or not isinstance(search_id, str) or not search_id):
        raise ValueError("搜索来源无效，请重新搜索")
    return site, keyword, search_id


async def _offload(operation: Callable[..., _PageResult], *args: Any, **kwargs: Any) -> _PageResult:
    """托管有限页任务，取消时等 worker 收尾，避免遗留请求。"""
    task = get_task_registry().create_sync(partial(operation, *args, **kwargs), owner="search.manual")
    try:
        return cast(_PageResult, await asyncio.shield(task))
    except asyncio.CancelledError:
        await await_task_to_terminal(task)
        raise


class _ManualPage:
    """一次单页请求只收集原始资源，结果处理委托给原搜索业务。"""

    def __init__(self, owner: _SearchOwnerBase, plan: MediaSearchPlan, raw_title: bool, search_id: str) -> None:
        self.owner, self.plan, self.raw_title = owner, plan, raw_title
        self.media = cast(MediaInfo, plan.mediainfo)
        self.sources = SearchSources(owner, plan)
        self.torrents: list[TorrentInfo] = []
        self.search_id = search_id
        self.page_cache = Cache(ttl=RESTART_AFTER_SECONDS)

    def read(self, key: str, page: int) -> dict[str, Any]:
        """上一页摘要保留 36 小时；失效时先补上一页再取目标页，只有目标页交给原结果处理。"""
        query = self.sources.queries[key]
        pageable = self.sources.pageable(key)
        source = encode_source(query["site"], query["keyword"], self.search_id)
        facts: dict[str, Any] = {"source": source,
                                "site_name": self.sources.sites.get(query["site"], {}).get("name"),
                                "page": page, "can_continue": False, "error": None}
        # 超过最大页数，或不支持分页的站点请求后续页时，直接按末页返回。
        if page >= MAX_SEARCH_PAGES or (page > 0 and not pageable):
            return facts
        previous = self.page_cache.get(f"{source}:{page - 1}", region=_PAGE_CACHE_REGION) if page else None
        pages = [page - 1, page] if page and previous is None else [page]
        for requested_page in pages:
            # 两次请求均经过原站点流控；失败不写摘要，也不推进客户端指定的页号。
            result = self.sources.fetch(key, requested_page)
            observation = result.observation
            if not (observation.attempted and observation.outcome == "success"):
                facts.update(error=observation.error or "本页请求未完成，请继续加载重试",
                             can_continue=query["site"] != "plugin")
                return facts
            signature = search_page_signature(search_resource_id(item) for item in result.torrents) or ""
            self.page_cache.set(f"{source}:{requested_page}", signature, region=_PAGE_CACHE_REGION)
            if requested_page < page:
                previous = signature
        raw_count = observation.raw_count if observation.raw_count is not None else len(result.torrents)
        if previous and signature == previous:
            return facts
        # 尚未确认末页时允许尝试下一页，不预取，也不保证下一页一定有结果。
        facts.update(can_continue=bool(
            pageable and raw_count and observation.has_more is not False and page + 1 < MAX_SEARCH_PAGES))
        self.torrents.extend(result.torrents)
        return facts

    def results(self) -> list[Context]:
        """复用原搜索的识别、季集过滤和排序；显示过滤与分页能力无关。"""
        if self.raw_title:
            return SearchTitleOwner._resolve_title_request(cast(SearchTitleOwner, self.owner), _TitleSearchResolveRequest(
                title=self.media.title, torrents=self.torrents, rule_groups=self.plan.rule_groups,
                mtype=None if self.media.type == MediaType.UNKNOWN else self.media.type,
            )).contexts
        return SearchResultOwner._parse_result(
            cast(SearchResultOwner, self.owner), torrents=self.torrents, mediainfo=self.media, keyword=self.plan.keyword,
            season_episodes=self.sources.season_episodes, rule_groups=self.plan.rule_groups,
            custom_words=self.plan.custom_words, filter_params=self.plan.filter_params,
        )


async def _prepare_page(owner: _SearchOwnerBase, params: dict[str, Any]) -> _ManualPage:
    """只构造本次请求的识别/关键词计划，不读写任何搜索检查点。"""
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


async def _page_events(manual: _ManualPage, params: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
    """每个来源只返回指定页；只有首页请求才使用别名回退和插件来源。"""
    page = max(0, int(params.get("page") or 0))
    initial = not params.get("source")
    if not initial:
        # 续页沿用来源标识里的实际关键词，不再经站点关键词转换，避免换成另一个来源。
        site, keyword, _search_id = decode_source(params["source"])
        manual.sources.queries = {f"{site}:{keyword}": {"site": site, "keyword": keyword}} if site in manual.sources.sites else {}
        keys = list(manual.sources.queries)
    else:
        keys = manual.sources.add_keywords(all_names=bool(manual.owner.runtime_config.search_multiple_name))
    facts: list[dict[str, Any]] = []
    contexts: list[Context] = []
    while keys:
        for key in keys:
            if key.startswith("plugin:") and not initial:
                continue
            facts.append(await _offload(manual.read, key, page))
            yield {"type": "progress", "stage": "searching", "value": 90,
                   "text": "已请求一页，正在处理其他搜索来源"}
        contexts = await _offload(manual.results)
        if contexts or not initial:
            break
        keys = manual.sources.add_keywords()
    if initial:
        # 首页沿用原搜索的结果缓存与上次搜索参数；续页不覆盖它们，累计结果由客户端持有。
        await manual.owner._async_save_results(contexts)
        manual.owner.save_last_search_params(
            keyword=params.get("keyword") if manual.raw_title else None,
            media_source=None if manual.raw_title else manual.media.media_source,
            media_id=None if manual.raw_title else manual.media.media_id, mtype=manual.media.type,
            area=manual.plan.area, season=None if manual.raw_title else params.get("season"),
            sites=params.get("sites"), result_type="torrent")
    items = [context.to_dict() for context in contexts]
    yield {"type": "replace", "stage": "filtered", "value": 100, "items": items,
           "total_items": len(items), "candidate_items": len(manual.torrents), "sources": facts}
    yield {"type": "done", "stage": "done", "total_items": len(items), "sources": facts}


class SearchManualOwner:
    """Web 单页结果与后端页摘要缓存，沿用既有站点、别名和媒体识别策略。"""

    @staticmethod
    async def events(owner: _SearchOwnerBase, *, params: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        """原 API 显式单页模式；首次请求各来源第 0 页，续页按来源标识请求指定页。"""
        try:
            manual = await _prepare_page(owner, params)
        except ValueError as error:
            yield {"type": "error", "message": str(error)}
            return
        async for event in _page_events(manual, params):
            yield event
