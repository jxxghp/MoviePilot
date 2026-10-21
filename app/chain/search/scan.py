"""
逐页搜索的用例编排：每页先收集，再决定续页或提交就绪目标。
"""

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
from app.application.subscription.activity import current_activity_run
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
from app.chain.search.provider import (
    _site_keyword,
    _site_rate_limit_backoff,
    _site_request_interval,
    _wait_for_site_request,
)
from app.chain.search.result import DisambiguationCache, SearchResultOwner, _resource_targets, stored_meta
from app.domain.context import Context, MediaInfo, TorrentInfo
from app.domain.episode import format_ranges
from app.domain.search import UNMATCHED_PAGE_LIMIT, SearchCollection, SearchSourceCursor
from app.runtime.log import logger
from app.runtime.stop import runtime_stop_state
from app.schemas.types import MediaType, SystemConfigKey

SEARCH_SLICE_SECONDS = 60
AUTOMATIC_REQUEST_INTERVAL = 10
# 检查点超过该时长未更新就丢弃旧进度重新搜索，已提交的目标保留。
RESTART_AFTER_SECONDS = 36 * 3600
# 当前检查点结构版本；兼容恢复 V2，无法识别的格式从第一页重新搜索。
_CHECKPOINT_FORMAT = 3
# 重取临时下载票据所在原页连续失败达到该次数后放弃该候选，避免一个候选卡住整个订阅。
RECOVERY_ATTEMPTS = 3
# 单个来源本轮因站点预算长冷却被推迟达到该次数后结算该来源：检查点随本轮结束删除，下一轮重新尝试，
# 既打破"全部来源结束才提交"的屏障死锁，也不永久拉黑偶尔不可用的站点。
BUDGET_DEFERRAL_LIMIT = 3
# 只累计不短于该秒数的冷却推迟；请求间隔与并发租约的秒级等待是健康站点噪声，不计入。
BUDGET_DEFERRAL_SECONDS = 600.0


def search_targets(plan: MediaSearchPlan) -> set[str]:
    """
    空 episodes 表示整季；不能将占位集数或未来日期当成真实缺集范围。
    """
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
    """
    原始页事实与临时等待独立，空列表本身不是末页证据。

    :param torrents: 本页原始资源；空列表不单独证明已到末页
    :param observation: 本次站点请求的成功、失败和分页观察事实
    :param retry_at: 因预算或流控延迟时允许恢复的 UTC 时间字符串，None 表示未延迟
    :param wait_reason: 需要暂缓本页请求的原因，None 表示未延迟
    """

    torrents: list[TorrentInfo]
    observation: SiteSearchObservation
    retry_at: Optional[str] = None
    wait_reason: Optional[str] = None


class SearchSources:
    """
    站点与实际关键词组成的搜索来源，以及单页请求；自动订阅与手动分页共用。
    """

    def __init__(self, owner: ChainBase, plan: MediaSearchPlan) -> None:
        """
        固定本轮站点与关键词，避免恢复分页时重新展开出重复查询。
        """
        self.activity = current_activity_run()
        self.owner = cast(_SearchOwnerBase, owner)
        self.media = cast(MediaInfo, plan.mediainfo)
        self.sites = {str(site["id"]): site for site in self.owner._sync_indexers(plan.sites)}
        if self.activity:
            self.activity.plan_sites(int(site_id) for site_id in self.sites)
        self.season_episodes, self.keywords = self.owner._prepare_params(
            mediainfo=plan.mediainfo, keyword=plan.keyword, no_exists=plan.no_exists)
        if plan.area == "imdbid" and plan.mediainfo.imdb_id:
            self.keywords = [plan.mediainfo.imdb_id]
        self.keyword_index = 0
        self.queries: dict[str, dict[str, Any]] = {}

    def add_keywords(self, *, all_names: bool = False) -> list[str]:
        """
        加入下一个（或全部）名称对应的来源；返回新增来源键。
        """
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
        """
        站点搜索入口是否支持分页；插件只有一页。
        """
        query = self.queries[key]
        if query["site"] == "plugin":
            return False
        return self.owner.get_search_page_size(site=self.sites[query["site"]], keyword=query["keyword"]) is not None

    def fetch(self, key: str, page: int, *, budget: Optional[SubscriptionSiteBudget] = None) -> ScanPage:
        """
        请求一个来源的指定页；自动订阅经站点预算限流，手动沿用站点既有请求间隔。
        """
        query = self.queries[key]
        # 标题搜索未指定类型时沿用 None；UNKNOWN 可能被站点解释为电影分区。
        media_type = None if self.media.type == MediaType.UNKNOWN else self.media.type
        if query["site"] == "plugin":
            items = self.owner.search_plugin_torrents(keyword=query["keyword"], mtype=media_type) or []
            if self.activity:
                self.activity.discover(len(items))
            return ScanPage(items, SiteSearchObservation(True, "success", raw_count=len(items), has_more=False))
        site = self.sites[query["site"]]
        claim = None
        if budget:
            try:
                claim = budget.acquire(int(query["site"]),
                                       minimum_interval=max(AUTOMATIC_REQUEST_INTERVAL, _site_request_interval(site)))
            except SubscriptionSiteBudgetUnavailable as error:
                if self.activity:
                    self.activity.site(error.site_id, name=str(site.get("name") or ""), status="waiting")
                return ScanPage([], SiteSearchObservation(error=str(error)), error.retry_at, error.wait_reason)
        else:
            _wait_for_site_request(site)
        with capture_site_search_observation() as observation:
            deferral_seconds = 0.0
            try:
                items = self.owner.search_site_torrents(site=site, keyword=query["keyword"],
                                                       mtype=media_type, page=page) or []
                if self.activity:
                    self.activity.site(int(query["site"]), name=str(site.get("name") or ""), candidates=len(items), status=observation.outcome, attempted=observation.attempted)
                if observation.outcome == "deferred":
                    # 本地流控未发出请求，按站点窗口退避，避免在限流窗口内以秒级间隔反复重试。
                    deferral_seconds = _site_rate_limit_backoff(site) or float(AUTOMATIC_REQUEST_INTERVAL)
                    retry_at = (datetime.now(timezone.utc) + timedelta(seconds=deferral_seconds)).isoformat(timespec="seconds")
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
                if self.activity:
                    self.activity.site(int(query["site"]), name=str(site.get("name") or ""), status="failed")
                return ScanPage([], observation)
            finally:
                if claim and budget:
                    budget.finish(claim, observation, deferral_seconds=deferral_seconds)


class SearchScan:
    """
    一轮订阅搜索持有的候选和来源游标，数据库在每页安全边界短暂使用。
    """

    def __init__(
        self, *, owner: ChainBase, plan: MediaSearchPlan, full: bool = False,
        execution: Optional[SubscriptionExecutionContext] = None,
        repository: Optional[SearchSessionRepository] = None,
        snapshot: Optional[SearchSessionSnapshot] = None,
        target_scope: Optional[tuple[int, int]] = None,
        page_limit: Optional[int] = None,
        prefer_complete: bool = False,
    ) -> None:
        """
        创建或恢复一轮搜索，候选与匹配前史由检查点一起持有。

        :param prefer_complete: 提交分集前先准备覆盖全部目标的候选；由订阅当前整包优先策略决定
        """
        if not isinstance(plan.mediainfo, MediaInfo):
            raise ValueError("分页影视搜索不适用于音乐")
        self.media = plan.mediainfo
        self.owner, self.plan, self.execution = cast(_SearchOwnerBase, owner), plan, execution
        self.repository, self.snapshot = repository, snapshot
        targets = search_targets(plan)
        # 整季缺失即使展开为逐集结算目标，也必须保留整季收集屏障。
        self.full = (full or any(target.endswith(":season") for target in targets)
                     or (self.media.type == MediaType.TV and any(
                         not missing.episodes for seasons in (plan.no_exists or {}).values()
                         for missing in seasons.values())))
        self.target_scope = target_scope
        self.page_limit = page_limit
        self.prefer_complete = prefer_complete
        self.ended = False
        self.restart = False
        self.collection = SearchCollection(targets)
        self.identity: dict[str, Any] = {}
        self.candidates: dict[str, Any] = {}
        self.live: dict[str, Context] = {}
        # 本轮已处理（过滤淘汰、已提交或下载失败）的候选，只在内存中，重启后重新判断；
        # 跨轮次的失败由原有下载失败冷却负责。
        self.attempted: set[str] = set()
        self.disambiguation: DisambiguationCache = {}
        self.retry: dict[str, str] = {}
        self.retry_reasons: dict[str, str] = {}
        # 候选原页重取的连续失败次数；站点等待不计入，只统计真实请求未成功。
        self.recovery_failures: dict[str, int] = {}
        # 本轮已提交下载的目标，仅用于进度展示。
        self.submitted: set[str] = set()
        self.sources = SearchSources(owner, plan)
        self._media_identity = encode_search_state({
            "source": self.media.media_source, "id": self.media.media_id, "type": self.media.type,
            "season": self.media.season, "episode_group": self.media.episode_group,
        })
        self._contract = self._contract_signature()
        # 最近一次写入的检查点内容（不含保存时间），内容未变时跳过重复写入。
        self._saved_body: Optional[str] = None
        if snapshot:
            data = json.loads(snapshot.payload)
            # V2 检查点没有查询匹配计数，恢复时保守沿用进度，不据缺失的前史提前停止。
            if data.get("format") in {2, _CHECKPOINT_FORMAT}:
                self._restore(data)
            else:
                self.restart = True
        else:
            self._add_sources(all_names=bool(self.owner.runtime_config.search_multiple_name))

    @property
    def queries(self) -> dict[str, dict[str, Any]]:
        """
        来源键到站点和实际关键词。
        """
        return self.sources.queries

    def _contract_signature(self, *, include_area: bool = True) -> str:
        """
        影响候选或范围的条件；兼容读取旧签名时搜索范围另由保存的关键词核对。
        """
        configuration = get_configured_system_config()
        media = self.media
        values = {"media": [media.media_source, media.media_id, media.type, media.title, media.season, media.episode_group],
                  "keyword": self.plan.keyword,
                  "custom_words": self.plan.custom_words,
                  "rules": self.plan.rule_groups_signature
                  if self.plan.rule_groups_signature is not None else self.plan.rule_groups,
                  "filter": self.plan.filter_params,
                  "sites": sorted(self.sources.sites), "full": self.full, "target_scope": self.target_scope,
                  "prefer_complete": self.prefer_complete,
                  "raw_title": False, "multiple_names": self.owner.runtime_config.search_multiple_name,
                  "identifiers": configuration.get(SystemConfigKey.CustomIdentifiers),
                  "release_groups": configuration.get(SystemConfigKey.CustomReleaseGroups),
                  "filter_groups": configuration.get(SystemConfigKey.UserFilterRuleGroups)}
        if include_area:
            values["area"] = self.plan.area
        if self.page_limit is not None:
            values["page_limit"] = self.page_limit
        return hashlib.sha256(encode_search_state(values).encode()).hexdigest()

    def _add_sources(self, *, all_names: bool = False) -> bool:
        """
        为新增名称查询创建独立游标，每个来源遵守同一页数预算。
        """
        added = self.sources.add_keywords(all_names=all_names)
        for key in added:
            self.collection.sources[key] = SearchSourceCursor(page_limit=self.page_limit)
        return bool(added)

    def _restore(self, data: dict[str, Any]) -> None:
        """
        校验搜索合同并恢复候选、匹配前史和连续页进度，已提交目标仍受媒体身份约束。
        """
        # 搜索条件变化或进度太久未更新时重新搜索。
        contract_matches = data.get("contract") == self._contract
        if data.get("format") == 2 and data.get("contract") == self._contract_signature(include_area=False):
            contract_matches = self._legacy_query_area_matches(data)
        self.restart = (not contract_matches
                        or time.time() - data.get("saved_at", 0) > RESTART_AFTER_SECONDS)
        self.collection = restore_collection(data["collection"])
        self.identity, self.candidates = data["identity"], data["candidates"]
        self.sources.queries = data["queries"]
        self.sources.keyword_index = data["keyword_index"]
        self.retry = data.get("retry", {})
        self.retry_reasons = data.get("retry_reasons", {})
        self.recovery_failures = data.get("recovery_failures", {})
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

    def _legacy_query_area_matches(self, data: dict[str, Any]) -> bool:
        """
        V2 签名未包含搜索范围，实际关键词必须与当前 IMDb/名称查询模式一致才能续搜。
        """
        keywords = {query["keyword"] for query in data["queries"].values() if query["site"] != "plugin"}
        was_imdb_query = bool(self.media.imdb_id) and keywords == {self.media.imdb_id}
        return was_imdb_query == (self.plan.area == "imdbid" and bool(self.media.imdb_id))

    def _restore_target_scope(self) -> None:
        """
        新增必要目标复用已识别元数据，补查头部后继续历史；已提交目标仍不重复。
        """
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
        """
        页候选、关闭状态及下一页作为同一检查点保存。

        只持久化仍覆盖剩余缺集的候选及其识别结果；不相关资源的识别缓存只留在本进程，
        恢复后再次遇到时重新识别，避免检查点随翻页无限增长。
        """
        remaining = self.collection.remaining
        candidates = {key: value for key, value in self.candidates.items()
                      if set(self.identity[key]["targets"] or []) & remaining}
        return {"format": _CHECKPOINT_FORMAT, "contract": self._contract, "media_identity": self._media_identity,
                "collection": collection_snapshot(self.collection),
                "identity": {key: self.identity[key] for key in candidates}, "candidates": candidates,
                "queries": self.queries,
                "keywords": self.sources.keywords, "keyword_index": self.sources.keyword_index,
                "retry": self.retry, "retry_reasons": self.retry_reasons,
                "recovery_failures": {key: count for key, count in self.recovery_failures.items() if key in candidates},
                "submitted": sorted(self.submitted), "saved_at": time.time()}

    def progress_text(self) -> str:
        """
        写入日志的进度摘要：提交、仍缺集和本轮结束分开表达，不把分页结束叫作补齐。
        """
        episodes = [int(target.split(":")[1]) for target in self.collection.remaining
                    if ":" in target and not target.endswith(":season")]
        remaining = format_ranges(episodes) or (("影片" if self.media.type == MediaType.MOVIE else "整季")
                                                 if self.collection.remaining else "无")
        submitted = format_ranges([int(target.split(":")[1]) for target in self.submitted
                                   if ":" in target and not target.endswith(":season")]) or "无"
        page = max((source.next_page for source in self.collection.sources.values()), default=0)
        unreleased = "" if self.full else format_ranges([int(target.split(":")[1]) for target in self.collection.unreleased()])
        waiting = f"（其中 {unreleased} 站点尚未收录，交给订阅模式追更）" if unreleased else ""
        if self.ended:
            failed = any(source.failed for source in self.collection.sources.values())
            return f"本轮搜索结束，已提交：{submitted}，仍缺：{remaining}{waiting}" + ("；部分来源失败" if failed else "")
        return f"已搜索到第 {page} 页，已提交：{submitted}，仍缺：{remaining}{waiting}"

    def _lost_ownership(self) -> None:
        """
        检查点写入被租约拒绝时立即交还执行，迟到 worker 不再提交候选。
        """
        if self.execution and self.execution.is_cancel_requested():
            raise SubscriptionSearchCancelled("搜索已停止")
        raise RuntimeError("搜索检查点所有权已失效")

    def checkpoint(self) -> None:
        """
        任务租约失效后停止运行，不能继续副作用或跳过尚未持久化的页面。
        """
        if not self.repository or not self.execution:
            return
        state = self.state()
        saved_at = state.pop("saved_at")
        body = encode_search_state(state)
        if self.snapshot is not None and body == self._saved_body:
            return
        payload = encode_search_state({**state, "saved_at": saved_at})
        lease = self.execution.task_lease
        if self.snapshot is None:
            saved = self.repository.create(task_id=self.execution.task_id or "", payload=payload, task_lease=lease)
        else:
            saved = self.repository.save(snapshot=self.snapshot, payload=payload, task_lease=lease)
        if saved is None:
            self._lost_ownership()
        self.snapshot = saved
        self._saved_body = body

    def _budget(self) -> Optional[SubscriptionSiteBudget]:
        """
        读取宿主注入的自动订阅预算，普通搜索不借用站点队列配额。
        """
        budget = getattr(self.owner, "_subscription_site_budget", None)
        return budget if isinstance(budget, SubscriptionSiteBudget) else None

    def _stop_unmatched_query(self, key: str) -> bool:
        """
        仅普通 IMDb 精确查询启用异常来源早停，全量、整季和深度兜底保留分页策略。
        """
        return (self.plan.area == "imdbid" and bool(self.media.imdb_id) and not self.full
                and not self.collection.fallback and not key.startswith("plugin:"))

    def step(self, key: str) -> bool:
        """
        执行来源的当前一页并先保存结果，再决定是否续页。
        """
        source = self.collection.sources[key]
        page = source.page_to_request(time.time())
        result = self.sources.fetch(key, page, budget=self._budget())
        observation = result.observation
        if result.retry_at:
            self._record_budget_wait(key, result)
            self.checkpoint()
            return False
        self.retry.pop(key, None)
        self.retry_reasons.pop(key, None)
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
        self.collection.observe(evidence)
        exhausted = key.startswith("plugin:") or observation.has_more is False or observation.raw_count == 0
        # 个别资源识别未知时本页仍然推进，只是不把本页当作任何目标的缺席证据。
        accepted = source.accept_page(page=page, evidence=evidence, targets=self.collection.remaining,
                                      exhausted=exhausted, now=time.time(), matched_work=bool(contexts),
                                      stop_unmatched=self._stop_unmatched_query(key))
        if accepted:
            if source.unmatched_pages == UNMATCHED_PAGE_LIMIT:
                logger.warning(f"{self.media.title_year} 搜索来源 {key} 连续 {UNMATCHED_PAGE_LIMIT} 页明确不匹配目标作品，"
                               "本轮停止该来源，继续处理其他来源候选")
            source.failure_outcome = None
            source.budget_deferrals = 0
            self.retry.pop(key, None)
            self.retry_reasons.pop(key, None)
            self.live.update(contexts)
            for candidate_id, context in contexts.items():
                self.candidates[candidate_id] = {"torrent": torrent_snapshot(context.torrent_info),
                                                  "source": key, "page": page}
            if not key.startswith("plugin:") and not self.sources.pageable(key):
                # 不支持分页的入口只检查第一页，按检查范围结束，不算来源失败。
                source.page_limit = 1
        self.checkpoint()
        return bool(accepted)

    def _record_budget_wait(self, key: str, result: ScanPage) -> None:
        """
        翻页与候选票据恢复共享来源长冷却计数，达到上限后仅停止本轮来源。

        :param key: 当前页或待恢复候选所属的搜索来源
        :param result: 携带重试时间和等待原因的页面结果
        """
        source = self.collection.sources[key]
        wait_reason = result.wait_reason or "busy"
        self.retry[key] = cast(str, result.retry_at)
        self.retry_reasons[key] = wait_reason
        source.error = result.observation.error
        if wait_reason == "cooldown":
            # 预算时间保存到秒，当前时间使用相同精度，避免恰好 600 秒的冷却被小数部分排除。
            deferral_seconds = (datetime.fromisoformat(cast(str, result.retry_at))
                                - datetime.now(timezone.utc).replace(microsecond=0)).total_seconds()
            if deferral_seconds >= BUDGET_DEFERRAL_SECONDS:
                source.budget_deferrals += 1
        if source.budget_deferrals >= BUDGET_DEFERRAL_LIMIT:
            source.failure_outcome = "deferred"
            source.failed = True
            self.retry.pop(key, None)
            self.retry_reasons.pop(key, None)
            logger.warning(f"{self.media.title_year} 搜索来源 {key} 本轮 {source.budget_deferrals} 次等待站点预算未果，"
                           "跳过该来源以提交已收集候选，下一轮将重新搜索")

    def _candidate_context(self, key: str) -> Optional[Context]:
        """
        取本进程的实时候选；重启后由站点模块按白名单快照恢复种子。
        """
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
            meta.type = self.media.type or MediaType.UNKNOWN
            context = Context(torrent_info=torrent, meta_info=meta, media_info=self.media,
                              resource_source="search", media_info_is_target=True, match_status="exact")
            self.live[key] = context
        if context.torrent_info is None:
            return None
        context.torrent_info.search_resource_id = key
        return context

    def _coverage(self, key: str) -> set[str]:
        """
        只返回候选仍覆盖的未提交目标，不重复结算已有下载。
        """
        return set(self.identity[key]["targets"] or []) & self.collection.remaining

    def _contexts(self, ready: set[str]) -> tuple[list[Context], set[str]]:
        """
        只恢复覆盖目标全部就绪的未尝试候选，整包不得绕过收集屏障。
        """
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
        """
        只重取当前最优待提交候选的原页，避免凭据不可持久化时反复重取已提交页面。
        """
        record = self.candidates[key]
        source, page = record["source"], record["page"]
        cursor = self.collection.sources[source]
        if cursor.budget_deferrals >= BUDGET_DEFERRAL_LIMIT:
            del self.candidates[key]
            self.checkpoint()
            return None
        retry_at = self.retry.get(source)
        if retry_at and retry_at > datetime.now(timezone.utc).isoformat(timespec="seconds"):
            self._defer(waiting_source=source)
        result = self.sources.fetch(source, page, budget=self._budget())
        if result.retry_at:
            self._record_budget_wait(source, result)
            if cursor.budget_deferrals < BUDGET_DEFERRAL_LIMIT:
                self._defer(waiting_source=source)
            # 临时票据尚未恢复时不能下载；放弃该候选，让仍可用的次优候选继续提交。
            del self.candidates[key]
            self.checkpoint()
            return None
        if result.observation.outcome != "success":
            failures = self.recovery_failures.get(key, 0) + 1
            if failures < RECOVERY_ATTEMPTS:
                self.recovery_failures[key] = failures
                self._defer()
            # 多次重取仍失败时放弃该候选，让排在后面的候选和兜底继续推进，下一轮搜索再重新发现。
            self.recovery_failures.pop(key, None)
            del self.candidates[key]
            logger.warning(f"{self.media.title_year} 候选资源所在页面连续 {failures} 次重取失败，本轮放弃该候选："
                           f"{result.observation.error or '请求未成功'}")
            self.checkpoint()
            return None
        cursor.budget_deferrals = 0
        self.recovery_failures.pop(key, None)
        self.retry.pop(source, None)
        self.retry_reasons.pop(source, None)
        _, contexts = SearchResultOwner._identify_page(
            torrents=result.torrents, mediainfo=self.media, targets=self.collection.targets,
            custom_words=self.plan.custom_words or [], cache=self.identity, disambiguation=self.disambiguation)
        for resource_id, context in contexts.items():
            if resource_id in self.candidates:
                if context.torrent_info is None:
                    continue
                context.torrent_info.search_resource_id = resource_id
                self.live[resource_id] = context
                self.candidates[resource_id]["torrent"] = torrent_snapshot(context.torrent_info)
        # 原页上已经没有的候选直接移除。
        for resource_id, candidate in list(self.candidates.items()):
            if (candidate["source"], candidate["page"]) == (source, page) and resource_id not in contexts:
                del self.candidates[resource_id]
        self.checkpoint()
        return self.live.get(key) if key in self.candidates else None

    def _filter(self, contexts: list[Context]) -> list[Context]:
        """
        按订阅规则、参数和候选校验过滤并排序。
        """
        return SearchResultOwner._filter_identified_contexts(
            cast(Any, self.owner), contexts, self.media, self.plan.rule_groups or [], self.plan.filter_params or {},
            self.plan.candidate_filter, season_episodes=self.sources.season_episodes,
        )

    def _prepared_batch(self, contexts: list[Context], ready: set[str]) -> tuple[list[Context], set[str]]:
        """
        整包优先时集中恢复覆盖全部目标的候选，其余场景沿用可用前缀分批。
        """
        ordered_keys = [cast(str, item.torrent_info.search_resource_id)
                        for item in contexts if item.torrent_info is not None]
        if self.prefer_complete:
            # 只调整整包与分集的先后，两类内部仍保留规则排序；不能在更早的
            # 缺票据分集处切批，导致后面的整包根本没有机会参与下载选择。
            contexts = sorted(contexts, key=lambda item: item.torrent_info is None or self._coverage(
                cast(str, item.torrent_info.search_resource_id)) != ready)
        batch: dict[str, Context] = {}
        for context in contexts:
            if context.torrent_info is None:
                continue
            key = cast(str, context.torrent_info.search_resource_id)
            # 前面的恢复重取原页时，可能已移除同页其他已失效的候选。
            if key not in self.candidates:
                continue
            context = self.live.get(key, context)
            if context.torrent_info is None:
                continue
            coverage = self._coverage(key)
            if not context.torrent_info.enclosure:
                # 全部整包候选须一起交给下载层校验；否则前一个不完整整包可能
                # 在后一个仍待恢复票据时，就被下载层按分集回退并提交。
                complete_pack = self.prefer_complete and coverage == ready
                if batch and not complete_pack:
                    break
                restored = self._recover_candidate(key)
                if (restored is None or restored.torrent_info is None
                        or not restored.torrent_info.enclosure or not self._filter([restored])):
                    # 恢复后的促销、标签等信息可能已变化，须重新通过过滤，不能沿用旧对象的筛选结论。
                    self.attempted.add(key)
                    continue
                context = restored
                coverage = self._coverage(key)
            if not coverage or not coverage <= ready:
                continue
            batch[key] = context
        # 恢复顺序只用于保证整包能参与选择，下载层回退时仍须沿用原有规则排序。
        prepared = [self.live.get(key, batch[key]) for key in ordered_keys if key in batch and key in self.candidates]
        goals = {target for item in prepared if item.torrent_info is not None
                 for target in self._coverage(cast(str, item.torrent_info.search_resource_id))}
        return prepared, goals

    def choose(self, ready: set[str], submit: Callable[[list[Context], set[str]], set[str]]) -> None:
        """
        合集覆盖目标全就绪才提交；剩余缓存先于共享游标兜底。
        """
        contexts, keys = self._contexts(ready)
        contexts = self._filter(contexts)
        accepted = {
            cast(str, context.torrent_info.search_resource_id)
            for context in contexts
            if context.torrent_info is not None
        }
        self.attempted.update(keys - accepted)
        batch, goals = self._prepared_batch(contexts, ready)
        settled = submit(batch, goals) if batch else set()
        self.attempted.update(
            cast(str, context.torrent_info.search_resource_id)
            for context in batch
            if context.torrent_info is not None
        )
        self.submitted.update(settled)
        self.collection.settle(settled)
        cached = {target for key, record in self.identity.items() if key in self.candidates and key not in self.attempted
                  for target in record["targets"] or []} & ready
        self.collection.deepen(ready - settled - cached)
        self.checkpoint()

    def _has_cached_ready(self) -> bool:
        """
        末页后仍有分批未提交的就绪候选时，先处理缓存，不能提前结束整轮。
        """
        ready = self.collection.ready(full=self.full)
        return any(key not in self.attempted and bool(coverage := self._coverage(key)) and coverage <= ready
                   for key in self.candidates)

    def _reset_changed_contract(self) -> None:
        """
        重搜仅保留经恢复时媒体身份校验的已提交目标。
        """
        self.collection = SearchCollection(search_targets(self.plan), settled=self.collection.settled)
        self.identity, self.candidates, self.live, self.retry, self.retry_reasons = {}, {}, {}, {}, {}
        self.recovery_failures = {}
        self.sources.queries = {}
        self.sources.keyword_index = 0
        self.attempted.clear()
        self.restart = False
        self._add_sources(all_names=bool(self.owner.runtime_config.search_multiple_name))
        self.checkpoint()

    def _defer(self, *, waiting_source: Optional[str] = None) -> None:
        """
        按仍可搜索来源的有效冷却时间交还队列，避免过期检查点让任务立即重领。

        :param waiting_source: 等待恢复候选票据的来源；即使已搜到末页也必须保留该来源的冷却
        """
        now = datetime.now(timezone.utc)
        now_at = now.isoformat(timespec="seconds")
        active_sources = self.collection.active_sources(full=self.full)
        if waiting_source is not None and waiting_source not in active_sources:
            active_sources.append(waiting_source)
        active = set(active_sources)
        self.retry = {key: retry_at for key, retry_at in self.retry.items()
                      if key in active and retry_at > now_at}
        self.retry_reasons = {key: reason for key, reason in self.retry_reasons.items() if key in self.retry}
        self.checkpoint()
        # 检查点只保存到秒，向上留一秒可确保实际等待不短于请求间隔。
        future_at = now + timedelta(seconds=AUTOMATIC_REQUEST_INTERVAL + 1)
        default_retry_at = future_at.replace(microsecond=0).isoformat(timespec="seconds")
        retry_at = min(self.retry.values(), default=default_retry_at)
        site_ids = tuple(int(self.queries[key]["site"]) for key in active_sources
                         if self.queries[key]["site"] != "plugin")
        reason = "cooldown" if self.retry and all(
            self.retry_reasons.get(key) == "cooldown" for key in self.retry) else "busy" if self.retry else "slice"
        logger.info(f"{self.media.title_year} 分页搜索暂停（{reason}），{retry_at} 后继续：{self.progress_text()}")
        raise SubscriptionSearchDeferred(retry_at=retry_at, site_ids=tuple(dict.fromkeys(site_ids)), wait_reason=reason)

    def _stopping(self) -> bool:
        """
        在页面和提交边界检查系统停机或当前任务的取消要求。
        """
        return runtime_stop_state.is_system_stopped or bool(self.execution and self.execution.should_stop())

    def _advance_sources(self, keys: list[str], submit: Callable[[list[Context], set[str]], set[str]],
                         started: float) -> bool:
        """
        各来源执行当前一页，处理后才续页；时间片在安全边界交还队列。
        """
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
        """
        本轮结束：删除检查点，避免已完成任务的候选与识别缓存长期留存；没有任何成功来源时沿用任务失败语义。
        """
        self.ended = True
        logger.info(f"{self.media.title_year} {self.progress_text()}")
        if self.repository and self.execution and self.snapshot:
            self.repository.delete(snapshot=self.snapshot, task_lease=self.execution.task_lease)
            self.snapshot = None
        sites = [source for key, source in self.collection.sources.items() if not key.startswith("plugin:")]
        plugin_result = any(source.found for key, source in self.collection.sources.items() if key.startswith("plugin:"))
        requested_failure = any(source.failure_outcome not in {None, "skipped"} for source in sites)
        if self.collection.remaining and requested_failure and sites and all(source.failed and not source.next_page for source in sites) and not plugin_result:
            raise SubscriptionSiteSearchFailed("；".join(dict.fromkeys(source.error or "站点搜索失败" for source in sites)))

    def run(self, submit: Callable[[list[Context], set[str]], set[str]]) -> None:
        """
        运行约 60 秒时间片，网络等待交还现有持久队列。
        """
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
                # 智能模式只剩站点未收录目标时不再尝试别名；完整模式仍保留别名的深页收集机会。
                if not (self.full or self.collection.pending) or not self._add_sources():
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
