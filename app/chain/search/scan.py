"""逐页搜索的用例编排：每页先收集，再决定续页或提交就绪目标。"""

import hashlib
import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional, cast

from app.application.configuration import get_configured_system_config
from app.application.search.session import (
    SearchSessionRepository,
    SearchSessionSnapshot,
    collection_snapshot,
    encode_search_state,
    restore_collection,
    torrent_snapshot,
)
from app.application.site.observation import SiteSearchObservation, capture_site_search_observation
from app.application.subscription.execution import SubscriptionExecutionContext, SubscriptionSiteSearchFailed
from app.application.subscription.sitebudget import (
    SubscriptionSearchCancelled,
    SubscriptionSearchDeferred,
    SubscriptionSiteBudget,
    SubscriptionSiteBudgetUnavailable,
)
from app.chain.base import ChainBase
from app.chain.search.contract import _SearchOwnerBase
from app.chain.search.execution import MediaSearchPlan
from app.chain.search.provider import _site_keyword, _site_request_interval, _wait_for_site_request
from app.chain.search.result import DisambiguationCache, SearchResultOwner, _resource_targets, stored_meta
from app.domain.context import Context, MediaInfo, TorrentInfo
from app.domain.episode import format_ranges
from app.domain.search import SearchCollection, SearchSourceCursor
from app.runtime.log import logger
from app.runtime.stop import runtime_stop_state
from app.schemas.types import MediaType, SystemConfigKey

SEARCH_SLICE_SECONDS = 60
AUTOMATIC_REQUEST_INTERVAL = 10
# 检查点超过该时长未更新就丢弃旧进度重新搜索，已提交的目标保留。
RESTART_AFTER_SECONDS = 36 * 3600


def search_targets(plan: MediaSearchPlan) -> set[str]:
    """空 episodes 表示整季；不能将占位集数或未来日期当成真实缺集范围。"""
    if plan.mediainfo.type == MediaType.MOVIE:
        return {"movie"}
    targets: set[str] = set()
    for seasons in (plan.no_exists or {}).values():
        for season, missing in seasons.items():
            episodes = list(missing.episodes or [])
            if not episodes and missing.total_episode and missing.total_episode < 9999:
                episodes = list(range(max(1, missing.start_episode or 1), missing.total_episode + 1))
            targets.update(f"{season}:{episode}" for episode in episodes)
            if not episodes:
                targets.add(f"{season}:season")
    season = plan.mediainfo.season if plan.mediainfo.season is not None else 1
    return targets or {f"{season}:season"}


@dataclass(frozen=True, slots=True)
class ScanPage:
    """原始页事实与临时等待独立，空列表本身不是末页证据。"""

    torrents: list[TorrentInfo]
    observation: SiteSearchObservation
    retry_at: Optional[str] = None
    wait_reason: Optional[str] = None


class SearchSources:
    """站点与实际关键词组成的搜索来源，以及单页请求；自动订阅与手动分页共用。"""

    def __init__(self, owner: ChainBase, plan: MediaSearchPlan) -> None:
        self.owner = cast(_SearchOwnerBase, owner)
        self.media = cast(MediaInfo, plan.mediainfo)
        self.sites = {str(site["id"]): site for site in self.owner._sync_indexers(plan.sites)}
        self.season_episodes, self.keywords = self.owner._prepare_params(
            mediainfo=plan.mediainfo, keyword=plan.keyword, no_exists=plan.no_exists)
        if plan.area == "imdbid" and plan.mediainfo.imdb_id:
            self.keywords = [plan.mediainfo.imdb_id]
        self.keyword_index = 0
        self.queries: dict[str, dict[str, Any]] = {}

    def add_keywords(self, *, all_names: bool = False) -> list[str]:
        """加入下一个（或全部）名称对应的来源；返回新增来源键。"""
        added: list[str] = []
        while self.keyword_index < len(self.keywords):
            keyword = self.keywords[self.keyword_index]
            self.keyword_index += 1
            for site_id, site in self.sites.items():
                actual = _site_keyword(site, keyword, self.media)
                key = f"{site_id}:{actual}"
                if key not in self.queries:
                    self.queries[key] = {"site": site_id, "keyword": actual}
                    added.append(key)
            plugin_key = f"plugin:{keyword}"
            if plugin_key not in self.queries:
                self.queries[plugin_key] = {"site": "plugin", "keyword": keyword}
                added.append(plugin_key)
            if not all_names:
                break
        return added

    def pageable(self, key: str) -> bool:
        """站点搜索入口是否支持分页；插件只有一页。"""
        query = self.queries[key]
        if query["site"] == "plugin":
            return False
        return self.owner.get_search_page_size(site=self.sites[query["site"]], keyword=query["keyword"]) is not None

    def fetch(self, key: str, page: int, *, budget: Optional[SubscriptionSiteBudget] = None) -> ScanPage:
        """请求一个来源的指定页；自动订阅经站点预算限流，手动沿用站点既有请求间隔。"""
        query = self.queries[key]
        # 标题搜索未指定类型时沿用 None；UNKNOWN 可能被站点解释为电影分区。
        media_type = None if self.media.type == MediaType.UNKNOWN else self.media.type
        if query["site"] == "plugin":
            items = self.owner.search_plugin_torrents(keyword=query["keyword"], mtype=media_type) or []
            return ScanPage(items, SiteSearchObservation(True, "success", raw_count=len(items), has_more=False))
        site = self.sites[query["site"]]
        claim = None
        if budget:
            try:
                claim = budget.acquire(int(query["site"]),
                                       minimum_interval=max(AUTOMATIC_REQUEST_INTERVAL, _site_request_interval(site)))
            except SubscriptionSiteBudgetUnavailable as error:
                return ScanPage([], SiteSearchObservation(error=str(error)), error.retry_at, error.wait_reason)
        else:
            _wait_for_site_request(site)
        with capture_site_search_observation() as observation:
            try:
                items = self.owner.search_site_torrents(site=site, keyword=query["keyword"],
                                                       mtype=media_type, page=page) or []
                if observation.outcome == "deferred":
                    retry_at = (datetime.now(timezone.utc) + timedelta(seconds=AUTOMATIC_REQUEST_INTERVAL)).isoformat(timespec="seconds")
                    return ScanPage(items, observation, retry_at, "cooldown")
                if claim and budget:
                    budget.record_request(claim.site_id, len(items))
                    if observation.outcome == "success":
                        budget.record_success(claim.site_id)
                return ScanPage(items, observation)
            except Exception as error:
                observation.attempted = True
                observation.outcome = "error"
                observation.error = str(error)
                return ScanPage([], observation)
            finally:
                if claim and budget:
                    budget.finish(claim, observation)


class SearchScan:
    """一轮订阅搜索持有的候选和来源游标，数据库在每页安全边界短暂使用。"""

    def __init__(
        self, *, owner: ChainBase, plan: MediaSearchPlan, full: bool = False,
        execution: Optional[SubscriptionExecutionContext] = None,
        repository: Optional[SearchSessionRepository] = None,
        snapshot: Optional[SearchSessionSnapshot] = None,
        target_scope: Optional[tuple[int, int]] = None,
        page_limit: Optional[int] = None,
    ) -> None:
        if not isinstance(plan.mediainfo, MediaInfo):
            raise ValueError("分页影视搜索不适用于音乐")
        self.media = plan.mediainfo
        self.owner, self.plan, self.execution = cast(_SearchOwnerBase, owner), plan, execution
        self.repository, self.snapshot = repository, snapshot
        self.full = full or any(target.endswith(":season") for target in search_targets(plan))
        self.target_scope = target_scope
        self.page_limit = page_limit
        self.ended = False
        self.restart = False
        self.collection = SearchCollection(search_targets(plan))
        self.identity: dict[str, Any] = {}
        self.candidates: dict[str, Any] = {}
        self.live: dict[str, Context] = {}
        # 本轮已处理（过滤淘汰、已提交或下载失败）的候选，只在内存中，重启后重新判断；
        # 跨轮次的失败由原有下载失败冷却负责。
        self.attempted: set[str] = set()
        self.disambiguation: DisambiguationCache = {}
        self.retry: dict[str, str] = {}
        self.retry_reasons: dict[str, str] = {}
        # 本轮已提交下载的目标，仅用于进度展示。
        self.submitted: set[str] = set()
        self.sources = SearchSources(owner, plan)
        self._media_identity = encode_search_state({
            "source": self.media.media_source, "id": self.media.media_id, "type": self.media.type,
            "season": self.media.season, "episode_group": self.media.episode_group,
        })
        self._contract = self._contract_signature()
        if snapshot:
            self._restore(json.loads(snapshot.payload))
        else:
            self._add_sources(all_names=bool(self.owner.runtime_config.search_multiple_name))

    @property
    def queries(self) -> dict[str, dict[str, Any]]:
        """来源键到站点和实际关键词。"""
        return self.sources.queries

    def _contract_signature(self) -> str:
        """影响候选或范围的条件；变化时旧进度不能继续使用。"""
        configuration = get_configured_system_config()
        media = self.media
        values = {"media": [media.media_source, media.media_id, media.type, media.title, media.season, media.episode_group],
                  "keyword": self.plan.keyword,
                  "custom_words": self.plan.custom_words, "rules": self.plan.rule_groups,
                  "filter": self.plan.filter_params,
                  "sites": sorted(self.sources.sites), "full": self.full, "target_scope": self.target_scope,
                  "raw_title": False, "multiple_names": self.owner.runtime_config.search_multiple_name,
                  "identifiers": configuration.get(SystemConfigKey.CustomIdentifiers),
                  "release_groups": configuration.get(SystemConfigKey.CustomReleaseGroups),
                  "filter_groups": configuration.get(SystemConfigKey.UserFilterRuleGroups)}
        if self.page_limit is not None:
            values["page_limit"] = self.page_limit
        return hashlib.sha256(encode_search_state(values).encode()).hexdigest()

    def _add_sources(self, *, all_names: bool = False) -> bool:
        added = self.sources.add_keywords(all_names=all_names)
        for key in added:
            self.collection.sources[key] = SearchSourceCursor(page_limit=self.page_limit)
        return bool(added)

    def _restore(self, data: dict[str, Any]) -> None:
        # 搜索条件变化或进度太久未更新时重新搜索。
        self.restart = (data.get("contract") != self._contract
                        or time.time() - data.get("saved_at", 0) > RESTART_AFTER_SECONDS)
        self.collection = restore_collection(data["collection"])
        self.identity, self.candidates = data["identity"], data["candidates"]
        self.sources.queries = data["queries"]
        self.sources.keyword_index = data["keyword_index"]
        self.retry = data.get("retry", {})
        self.retry_reasons = data.get("retry_reasons", {})
        self.submitted = set(data.get("submitted", []))
        same_media = data["media_identity"] == self._media_identity
        if not same_media:
            self.restart = True
            self.collection.settled.clear()
            self.submitted.clear()
        if not self.restart:
            self.collection.settle(self.collection.targets - search_targets(self.plan))
            self._restore_target_scope()
            self.sources.keywords = data.get("keywords", self.sources.keywords)

    def _restore_target_scope(self) -> None:
        """新增必要目标复用已识别元数据，补查头部后继续历史；已提交目标仍不重复。"""
        fresh = search_targets(self.plan) - self.collection.targets
        if not fresh:
            return
        self.collection.targets.update(fresh)
        for record in self.identity.values():
            if record["source"] and record["targets"] is not None:
                record["targets"] = sorted(_resource_targets(stored_meta(record), self.collection.targets, self.media.type))
        for source in self.collection.sources.values():
            if not source.exhausted and not source.failed:
                source.last_request_at = 0

    def state(self) -> dict[str, Any]:
        """页候选、关闭状态及下一页作为同一检查点保存。"""
        return {"contract": self._contract, "media_identity": self._media_identity,
                "collection": collection_snapshot(self.collection),
                "identity": self.identity, "candidates": self.candidates, "queries": self.queries,
                "keywords": self.sources.keywords, "keyword_index": self.sources.keyword_index,
                "retry": self.retry, "retry_reasons": self.retry_reasons,
                "submitted": sorted(self.submitted), "saved_at": time.time()}

    def progress_text(self) -> str:
        """写入日志的进度摘要：提交、仍缺集和本轮结束分开表达，不把分页结束叫作补齐。"""
        episodes = [int(target.split(":")[1]) for target in self.collection.remaining
                    if ":" in target and not target.endswith(":season")]
        remaining = format_ranges(episodes) or (("影片" if self.media.type == MediaType.MOVIE else "整季")
                                                 if self.collection.remaining else "无")
        submitted = format_ranges([int(target.split(":")[1]) for target in self.submitted
                                   if ":" in target and not target.endswith(":season")]) or "无"
        page = max((source.next_page for source in self.collection.sources.values()), default=0)
        if self.ended:
            failed = any(source.failed for source in self.collection.sources.values())
            return f"本轮搜索结束，已提交：{submitted}，仍缺：{remaining}" + ("；部分来源失败" if failed else "")
        return f"已搜索到第 {page} 页，已提交：{submitted}，仍缺：{remaining}"

    def _lost_ownership(self) -> None:
        if self.execution and self.execution.is_cancel_requested():
            raise SubscriptionSearchCancelled("搜索已停止")
        raise RuntimeError("搜索检查点所有权已失效")

    def checkpoint(self) -> None:
        """任务租约失效后停止运行，不能继续副作用或跳过尚未持久化的页面。"""
        if not self.repository or not self.execution:
            return
        payload = encode_search_state(self.state())
        lease = self.execution.task_lease
        if self.snapshot is None:
            saved = self.repository.create(task_id=self.execution.task_id or "", payload=payload, task_lease=lease)
        else:
            saved = self.repository.save(snapshot=self.snapshot, payload=payload, task_lease=lease)
        if saved is None:
            self._lost_ownership()
        self.snapshot = saved

    def _budget(self) -> Optional[SubscriptionSiteBudget]:
        budget = getattr(self.owner, "_subscription_site_budget", None)
        return budget if isinstance(budget, SubscriptionSiteBudget) else None

    def step(self, key: str) -> bool:
        """执行来源的当前一页并先保存结果，再决定是否续页。"""
        source = self.collection.sources[key]
        page = source.page_to_request(time.time())
        result = self.sources.fetch(key, page, budget=self._budget())
        observation = result.observation
        if result.retry_at:
            self.retry[key] = result.retry_at
            self.retry_reasons[key] = result.wait_reason or "busy"
            source.error = observation.error
            self.checkpoint()
            return False
        if not observation.attempted or observation.outcome != "success":
            source.error = observation.error or "本页没有成功请求，保留页号"
            if source.head_refresh:
                # 补拉第一页可以放弃：不关闭来源，下次回到原历史页；站点冷却由预算记录的失败退避负责。
                source.cancel_head_refresh()
                self.checkpoint()
                return False
            source.failure_outcome = observation.outcome
            source.failed = True
            source.cancel_head_refresh()
            self.checkpoint()
            return False
        evidence, contexts = SearchResultOwner._identify_page(
            torrents=result.torrents, mediainfo=self.media, targets=self.collection.targets,
            custom_words=self.plan.custom_words or [], cache=self.identity, disambiguation=self.disambiguation,
        )
        exhausted = key.startswith("plugin:") or observation.has_more is False or observation.raw_count == 0
        # 个别资源识别未知时本页仍然推进，只是不把本页当作任何目标的缺席证据。
        accepted = source.accept_page(page=page, evidence=evidence, targets=self.collection.remaining,
                                      exhausted=exhausted, now=time.time())
        if accepted:
            source.failure_outcome = None
            self.retry.pop(key, None)
            self.retry_reasons.pop(key, None)
            self.live.update(contexts)
            for candidate_id, context in contexts.items():
                self.candidates[candidate_id] = {"torrent": torrent_snapshot(context.torrent_info),
                                                  "source": key, "page": page}
            if not key.startswith("plugin:") and not self.sources.pageable(key):
                source.failed = True
                source.error = "此站点搜索入口不支持分页，已保留第一页结果"
        self.checkpoint()
        return bool(accepted)

    def _candidate_context(self, key: str) -> Optional[Context]:
        """取本进程的实时候选；重启后由站点模块按白名单快照恢复种子。"""
        context = self.live.get(key)
        if context is None:
            saved = self.candidates[key]
            site_id = self.queries[saved["source"]]["site"]
            if site_id == "plugin":
                torrent = TorrentInfo()
                torrent.from_dict(saved["torrent"])
            else:
                torrent = self.owner.run_module("restore_search_torrent", site=self.sources.sites.get(site_id, {}),
                                                record=saved["torrent"], allow_missing=True)
            if not torrent:
                return None
            meta = stored_meta(self.identity[key])
            meta.type = self.media.type
            context = Context(torrent_info=torrent, meta_info=meta, media_info=self.media,
                              resource_source="search", media_info_is_target=True, match_status="exact")
            self.live[key] = context
        context.torrent_info.search_resource_id = key
        return context

    def _coverage(self, key: str) -> set[str]:
        return set(self.identity[key]["targets"] or []) & self.collection.remaining

    def _contexts(self, ready: set[str]) -> tuple[list[Context], set[str]]:
        contexts, keys = [], set()
        for key in self.candidates:
            coverage = self._coverage(key)
            if key in self.attempted or not coverage or not coverage <= ready:
                continue
            context = self._candidate_context(key)
            if context is None:
                continue
            contexts.append(context)
            keys.add(key)
        return contexts, keys

    def _recover_candidate(self, key: str) -> Optional[Context]:
        """只重取当前最优待提交候选的原页，避免凭据不可持久化时反复重取已提交页面。"""
        record = self.candidates[key]
        source, page = record["source"], record["page"]
        result = self.sources.fetch(source, page, budget=self._budget())
        if result.retry_at:
            self.retry[source] = result.retry_at
            self.retry_reasons[source] = result.wait_reason or "busy"
            self._defer()
        if result.observation.outcome != "success":
            self._defer()
        self.retry.pop(source, None)
        self.retry_reasons.pop(source, None)
        _, contexts = SearchResultOwner._identify_page(
            torrents=result.torrents, mediainfo=self.media, targets=self.collection.targets,
            custom_words=self.plan.custom_words or [], cache=self.identity, disambiguation=self.disambiguation)
        for resource_id, context in contexts.items():
            if resource_id in self.candidates:
                context.torrent_info.search_resource_id = resource_id
                self.live[resource_id] = context
                self.candidates[resource_id]["torrent"] = torrent_snapshot(context.torrent_info)
        # 原页上已经没有的候选直接移除。
        for resource_id, candidate in list(self.candidates.items()):
            if (candidate["source"], candidate["page"]) == (source, page) and resource_id not in contexts:
                del self.candidates[resource_id]
        self.checkpoint()
        return self.live.get(key) if key in self.candidates else None

    def _prepared_batch(self, contexts: list[Context], ready: set[str]) -> tuple[list[Context], set[str]]:
        """保留排序优先级，先提交可用前缀，再刷新下一个临时票据所在原页。"""
        batch: list[Context] = []
        goals: set[str] = set()
        for context in contexts:
            key = cast(str, context.torrent_info.search_resource_id)
            context = self.live.get(key, context)
            if not context.torrent_info.enclosure:
                if batch:
                    break
                restored = self._recover_candidate(key)
                if restored is None:
                    continue
                context = restored
            coverage = self._coverage(key)
            if not coverage or not coverage <= ready:
                continue
            batch.append(context)
            goals.update(coverage)
        return batch, goals

    def choose(self, ready: set[str], submit: Callable[[list[Context], set[str]], set[str]]) -> None:
        """合集覆盖目标全就绪才提交；剩余缓存先于共享游标兜底。"""
        contexts, keys = self._contexts(ready)
        contexts = SearchResultOwner._filter_identified_contexts(
            cast(Any, self.owner), contexts, self.media, self.plan.rule_groups or [], self.plan.filter_params or {},
            self.plan.candidate_filter, season_episodes=self.sources.season_episodes,
        )
        accepted = {cast(str, context.torrent_info.search_resource_id) for context in contexts}
        self.attempted.update(keys - accepted)
        batch, goals = self._prepared_batch(contexts, ready)
        settled = submit(batch, goals) if batch else set()
        self.attempted.update(cast(str, context.torrent_info.search_resource_id) for context in batch)
        self.submitted.update(settled)
        self.collection.settle(settled)
        cached = {target for key, record in self.identity.items() if key in self.candidates and key not in self.attempted
                  for target in record["targets"] or []} & ready
        self.collection.deepen(ready - settled - cached)
        self.checkpoint()

    def _has_cached_ready(self) -> bool:
        """末页后仍有分批未提交的就绪候选时，先处理缓存，不能提前结束整轮。"""
        ready = self.collection.ready(full=self.full)
        return any(key not in self.attempted and bool(coverage := self._coverage(key)) and coverage <= ready
                   for key in self.candidates)

    def _reset_changed_contract(self) -> None:
        """重搜仅保留经恢复时媒体身份校验的已提交目标。"""
        self.collection = SearchCollection(search_targets(self.plan), settled=self.collection.settled)
        self.identity, self.candidates, self.live, self.retry, self.retry_reasons = {}, {}, {}, {}, {}
        self.sources.queries = {}
        self.sources.keyword_index = 0
        self.attempted.clear()
        self.restart = False
        self._add_sources(all_names=bool(self.owner.runtime_config.search_multiple_name))
        self.checkpoint()

    def _defer(self) -> None:
        self.checkpoint()
        future = (datetime.now(timezone.utc) + timedelta(seconds=AUTOMATIC_REQUEST_INTERVAL)).isoformat(timespec="seconds")
        retry_at = min(self.retry.values(), default=future)
        site_ids = tuple(int(self.queries[key]["site"]) for key in self.collection.active_sources(full=self.full)
                         if self.queries[key]["site"] != "plugin")
        reason = "cooldown" if self.retry and all(
            self.retry_reasons.get(key) == "cooldown" for key in self.retry) else "busy" if self.retry else "slice"
        logger.info(f"{self.media.title_year} 分页搜索暂停（{reason}），{retry_at} 后继续：{self.progress_text()}")
        raise SubscriptionSearchDeferred(retry_at=retry_at, site_ids=tuple(dict.fromkeys(site_ids)), wait_reason=reason)

    def _stopping(self) -> bool:
        return runtime_stop_state.is_system_stopped or bool(self.execution and self.execution.should_stop())

    def _advance_sources(self, keys: list[str], submit: Callable[[list[Context], set[str]], set[str]],
                         started: float) -> bool:
        """各来源执行当前一页，处理后才续页；时间片在安全边界交还队列。"""
        advanced = False
        for key in keys:
            if self._stopping():
                return advanced
            retry_at = self.retry.get(key)
            if retry_at and retry_at > datetime.now(timezone.utc).isoformat(timespec="seconds"):
                continue
            advanced = self.step(key) or advanced
            ready = self.collection.ready(full=self.full)
            if ready:
                self.choose(ready, submit)
            if self.execution and time.monotonic() - started >= SEARCH_SLICE_SECONDS:
                self._defer()
        return advanced

    def _finish_round(self) -> None:
        """本轮结束：删除检查点，避免已完成任务的候选与识别缓存长期留存；没有任何成功来源时沿用任务失败语义。"""
        self.ended = True
        logger.info(f"{self.media.title_year} {self.progress_text()}")
        if self.repository and self.execution and self.snapshot:
            self.repository.delete(snapshot=self.snapshot, task_lease=self.execution.task_lease)
            self.snapshot = None
        sites = [source for key, source in self.collection.sources.items() if not key.startswith("plugin:")]
        plugin_result = any(source.resources for key, source in self.collection.sources.items() if key.startswith("plugin:"))
        requested_failure = any(source.failure_outcome not in {None, "skipped"} for source in sites)
        if self.collection.remaining and requested_failure and sites and all(source.failed and not source.next_page for source in sites) and not plugin_result:
            raise SubscriptionSiteSearchFailed("；".join(dict.fromkeys(source.error or "站点搜索失败" for source in sites)))

    def run(self, submit: Callable[[list[Context], set[str]], set[str]]) -> None:
        """运行约 60 秒时间片，网络等待交还现有持久队列。"""
        started = time.monotonic()
        if self.restart:
            self._reset_changed_contract()
        while self.collection.remaining:
            if self._stopping():
                return
            keys = self.collection.active_sources(full=self.full)
            if not keys:
                self.choose(self.collection.ready(full=self.full), submit)
                if self._has_cached_ready() or self.collection.active_sources(full=self.full):
                    continue
                if not self.collection.remaining or not self._add_sources():
                    self._finish_round()
                    return
                self.checkpoint()
                continue
            advanced = self._advance_sources(keys, submit, started)
            if self._stopping():
                return
            if not advanced:
                if self.execution:
                    self._defer()
                return
        self._finish_round()
