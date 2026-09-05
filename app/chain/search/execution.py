"""所有媒体类型共用的资源搜索状态机与同步、异步 I/O 驱动。"""

import asyncio
import random
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Dict, Generator, Iterator, List, Literal, Optional, cast

from app.chain.media import MediaChain
from app.chain.search.contract import _SearchOwnerBase
from app.chain.search.plan import SearchPlanOwner
from app.domain.context import Context, MediaInfo, MusicInfo, TorrentInfo
from app.domain.metainfo import MetaInfo
from app.runtime.execution import run_in_threadpool
from app.runtime.log import logger
from app.schemas.mediaserver import NotExistMediaInfo


@dataclass(frozen=True, slots=True)
class MediaSearchPlan:
    """冻结搜索业务输入，媒体类型只在关键词和匹配策略中产生差异。"""

    mediainfo: MediaInfo | MusicInfo
    keyword: Optional[str] = None
    no_exists: Optional[Dict[str, Dict[int, NotExistMediaInfo]]] = None
    sites: Optional[List[int]] = None
    rule_groups: Optional[List[str]] = None
    area: Optional[str] = "title"
    custom_words: Optional[List[str]] = None
    filter_params: Optional[Dict[str, str]] = None
    include_candidates: bool = False
    candidate_filter: Optional[Callable[[List[Context]], List[Context]]] = None


@dataclass(frozen=True, slots=True)
class _SearchStep:
    """请求驱动器执行一次 I/O 或 CPU 操作，不在外壳重复媒体业务决策。"""

    kind: Literal["recognize", "supplement", "search", "parse"]
    params: Dict[str, Any]
    search_count: int = 0


@dataclass(frozen=True, slots=True)
class _SearchOutcome:
    """记录最终结果、原始候选数量及各阶段诊断。"""

    contexts: List[Context]
    candidate_count: int = 0
    counts: Dict[str, int] = field(default_factory=dict)
    recognition_failed: bool = False


def _result_params(plan: MediaSearchPlan, mediainfo: MediaInfo | MusicInfo,
                   torrents: List[TorrentInfo], season_episodes: Any, counts: Counter[str]) -> Dict[str, Any]:
    """为每次完整过滤建立统一参数，计数只描述当前累计候选而不重复叠加。"""
    counts.clear()
    return {
        "torrents": torrents, "mediainfo": mediainfo, "keyword": plan.keyword,
        "rule_groups": plan.rule_groups, "season_episodes": season_episodes,
        "custom_words": plan.custom_words, "filter_params": plan.filter_params,
        "include_candidates": plan.include_candidates, "diagnostics": counts,
        "candidate_filter": plan.candidate_filter,
    }


def _search_resolution(owner: _SearchOwnerBase, plan: MediaSearchPlan) -> Generator[_SearchStep, Any, _SearchOutcome]:
    """统一准备、换词、最终过滤和提前停止，音乐没有独立的搜索循环。"""
    mediainfo = SearchPlanOwner._prepare_media_input(owner._copy_media_input(plan.mediainfo))
    logger.info(f"开始搜索资源，关键词：{plan.keyword or mediainfo.title} ...")
    if SearchPlanOwner._needs_media_details(mediainfo):
        mediainfo = yield _SearchStep("recognize", {
            "mtype": mediainfo.type, **owner._media_recognize_kwargs(mediainfo),
        })
        if not mediainfo:
            return _SearchOutcome([], recognition_failed=True)
    mediainfo = (yield _SearchStep("supplement", {"mediainfo": mediainfo})) or mediainfo
    prepare_params = {
        "mediainfo": mediainfo, "keyword": plan.keyword, "no_exists": plan.no_exists,
    }
    if plan.include_candidates:
        prepare_params["include_candidates"] = True
    season_episodes, keywords = owner._prepare_params(**prepare_params)
    torrents: List[TorrentInfo] = []
    contexts: List[Context] = []
    counts: Counter[str] = Counter()
    parsed = False
    for index, keyword in enumerate(keywords):
        batch = yield _SearchStep("search", {
            "mediainfo": mediainfo, "keyword": keyword, "sites": plan.sites, "area": plan.area,
        }, search_count=index)
        if not batch:
            continue
        torrents.extend(batch)
        contexts = yield _SearchStep("parse", _result_params(plan, mediainfo, torrents, season_episodes, counts))
        parsed = True
        confirmed = any(getattr(context, "match_status", None) in (None, "exact") for context in contexts)
        if confirmed and not owner.runtime_config.search_multiple_name:
            logger.info(f"共搜索到 {len(contexts)} 个可用资源，停止搜索")
            break
    if not parsed:
        contexts = yield _SearchStep("parse", _result_params(plan, mediainfo, torrents, season_episodes, counts))
    return _SearchOutcome(contexts, candidate_count=len(torrents), counts=dict(counts))


def _candidate_contexts(mediainfo: MediaInfo | MusicInfo, torrents: List[TorrentInfo]) -> List[Context]:
    """原始预览仅展示资源自身解析信息，不提前绑定未经匹配的目标媒体。"""
    return [Context(
        meta_info=MetaInfo(title=torrent.title, subtitle=torrent.description, mtype=mediainfo.type),
        torrent_info=torrent, resource_source="search", media_info_is_target=False,
        match_status="candidate", match_reason="unverified",
    ) for torrent in torrents]


class SearchExecutionOwner:
    """通过相同的业务状态机驱动三种 I/O 模式，不按媒体类型重复实现。"""

    @staticmethod
    def run(owner: _SearchOwnerBase, plan: MediaSearchPlan, media_chain: MediaChain) -> List[Context]:
        """同步驱动统一状态机，网络请求和结果解析均在当前同步调用中执行。"""
        flow = _search_resolution(owner, plan)
        response: Any = None
        while True:
            try:
                step = flow.send(response)
            except StopIteration as completed:
                outcome = cast(_SearchOutcome, completed.value)
                SearchExecutionOwner._log_outcome(outcome)
                return outcome.contexts
            if step.kind == "recognize":
                response = media_chain.recognize_media(**step.params)
            elif step.kind == "supplement":
                response = media_chain.supplement_media_info(**step.params)
            elif step.kind == "search":
                if step.search_count:
                    time.sleep(random.randint(1, 10))
                response = owner._SearchChain__search_all_sites(**step.params) or []
            else:
                response = owner._parse_result(**step.params)

    @staticmethod
    def _log_outcome(outcome: _SearchOutcome) -> None:
        """以一致口径报告搜索失败或原始召回与最终保留数量。"""
        if outcome.recognition_failed:
            logger.error("媒体信息识别失败！")
        else:
            logger.info(f"搜索返回 {outcome.candidate_count} 个候选，保留 {len(outcome.contexts)} 个，过滤匹配统计：{outcome.counts}")

    @staticmethod
    async def _provider_events(owner: _SearchOwnerBase, step: _SearchStep,
                               streaming: bool) -> AsyncIterator[Dict[str, Any]]:
        """只在 I/O 适配层区分普通请求和站点事件流。"""
        if step.search_count:
            await asyncio.sleep(random.randint(1, 10))
        if streaming:
            async for event in owner._SearchChain__async_search_all_sites_stream(**step.params):
                yield event
        else:
            yield {"items": await owner._SearchChain__async_search_all_sites(**step.params) or []}

    @staticmethod
    async def events(owner: _SearchOwnerBase, plan: MediaSearchPlan, media_chain: MediaChain,
                     streaming: bool = True) -> AsyncIterator[Dict[str, Any]]:
        """异步和 SSE 复用同一驱动器，只有是否向调用方发送中间进度不同。"""
        flow = _search_resolution(owner, plan)
        response: Any = None
        raw_count = 0
        while True:
            try:
                step = flow.send(response)
            except StopIteration as completed:
                for event in SearchExecutionOwner._completion_events(cast(_SearchOutcome, completed.value), streaming):
                    yield event
                return
            if step.kind == "recognize":
                response = await media_chain.async_recognize_media(**step.params)
            elif step.kind == "supplement":
                response = await media_chain.async_supplement_media_info(**step.params)
            elif step.kind == "search":
                batch: List[TorrentInfo] = []
                async for event in SearchExecutionOwner._provider_events(owner, step, streaming):
                    items = event.pop("items", []) or []
                    batch.extend(items)
                    raw_count += len(items)
                    if streaming:
                        previews = await run_in_threadpool(_candidate_contexts, step.params["mediainfo"], items)
                        yield {
                            **event, "type": "append", "stage": "searching",
                            "items": [context.to_dict() for context in previews],
                            "total_items": raw_count, "candidate_items": raw_count,
                        }
                response = batch
            else:
                if streaming:
                    yield {
                        "type": "progress", "stage": "filtering", "value": 98,
                        "text": f"正在过滤匹配 {len(step.params['torrents'])} 个候选资源 ...",
                    }
                response = await run_in_threadpool(owner._parse_result, **step.params)
    @staticmethod
    def _completion_events(outcome: _SearchOutcome, streaming: bool) -> Iterator[Dict[str, Any]]:
        """只在状态机正常完成后统一发布结果，失败和提前关闭不会伪造完成状态。"""
        SearchExecutionOwner._log_outcome(outcome)
        if outcome.recognition_failed:
            yield {"type": "error", "success": False, "message": "媒体信息识别失败"}
            return
        summary = {
            "items": [context.to_dict() for context in outcome.contexts],
            "total_items": len(outcome.contexts), "candidate_items": outcome.candidate_count,
            "match_counts": outcome.counts,
        }
        if streaming:
            yield {
                **summary, "type": "replace", "stage": "filtered", "value": 100,
                "text": f"过滤匹配完成，共 {len(outcome.contexts)} 个资源",
            }
        yield {
            **summary, "type": "done", "stage": "done", "contexts": outcome.contexts,
            "text": f"搜索完成，共 {len(outcome.contexts)} 个资源",
        }
