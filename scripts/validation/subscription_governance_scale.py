#!/usr/bin/env python3
"""运行订阅执行治理的确定性规模验收并输出 JSON 证据。"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import platform
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional
from unittest.mock import patch

from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import Session, sessionmaker

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.application.site.observation import report_site_search_outcome  # noqa: E402
from app.application.subscription.candidates import CandidateIndex  # noqa: E402
from app.application.subscription.contract import SubscriptionSnapshot  # noqa: E402
from app.application.subscription.execution import SubscriptionExecutionAdmission  # noqa: E402
from app.application.subscription.facts import FreshFactLease  # noqa: E402
from app.application.subscription.sitebudget import (  # noqa: E402
    SubscriptionSiteBudget as SubscriptionSiteBudgetController,
)
from app.chain.download import batch as download_batch  # noqa: E402
from app.chain.download.facade import DownloadChain  # noqa: E402
from app.chain.search.facade import SearchChain  # noqa: E402
from app.chain.subscribe import match as subscribe_match  # noqa: E402
from app.chain.subscribe import policy as subscribe_policy  # noqa: E402
from app.chain.subscribe.facade import SubscribeChain  # noqa: E402
from app.db.adapters.subscriptionsearch import (  # noqa: E402
    TransactionalSubscriptionSearchRepository,
)
from app.db.base import Base  # noqa: E402
from app.db.models.subscriptionsearch import (  # noqa: E402
    SubscriptionSearchTask,
)
from app.db.models.subscriptionsearch import (  # noqa: E402
    SubscriptionSiteBudget as SubscriptionSiteBudgetRecord,
)
from app.domain.context import Context, MediaInfo, TorrentInfo  # noqa: E402
from app.domain.metainfo import MetaInfo  # noqa: E402
from app.runtime.stop import ProcessStopState  # noqa: E402
from app.schemas.mediaserver import NotExistMediaInfo  # noqa: E402
from app.schemas.types import MediaSource, MediaType  # noqa: E402


@dataclass(frozen=True, slots=True)
class ScaleCase:
    """声明一档订阅、站点、候选和唯一媒体规模。"""

    name: str
    subscription_count: int
    site_count: int
    candidate_count: int
    media_count: int


def _percentile(values: list[float], percentile: int) -> float:
    """用 nearest-rank 计算稳定百分位，空集合返回零。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    position = max(0, math.ceil(len(ordered) * percentile / 100) - 1)
    return round(ordered[position], 3)


def _git_revision() -> str:
    """返回当前工作树 HEAD，脚本失败时保留 unknown。"""
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=PROJECT_ROOT,
            text=True,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _context(*, candidate_index: int, media_id: str, site_id: int) -> Context:
    """构造同时携带 canonical 媒体身份和稳定资源身份的候选。"""
    title = f"Governance Show {media_id} S01E01"
    meta = MetaInfo(title)
    meta.type = MediaType.TV
    meta.begin_season = 1
    meta.begin_episode = 1
    meta.end_episode = None
    meta.media_source = MediaSource.TMDB
    meta.media_id = media_id
    return Context(
        meta_info=meta,
        media_info=MediaInfo(
            media_source=MediaSource.TMDB,
            media_id=media_id,
            type=MediaType.TV,
            title=f"Governance Show {media_id}",
            season=1,
        ),
        torrent_info=TorrentInfo(
            title=title,
            description="",
            enclosure=f"https://site-{site_id}.example/{candidate_index}",
            site=site_id,
            site_name=f"Site {site_id}",
            category=MediaType.TV.value,
        ),
        resource_source="rss",
        match_source=MediaSource.TMDB.value,
        candidate_recognized=True,
    )


def _subscription(subscription_id: int, media_id: str, site_count: int) -> SubscriptionSnapshot:
    """构造覆盖全部受控站点的同季电视剧订阅。"""
    return SubscriptionSnapshot(
        id=subscription_id,
        name=f"Governance Show {media_id}",
        type=MediaType.TV.value,
        media_source=MediaSource.TMDB,
        media_id=media_id,
        season=1,
        total_episode=2,
        start_episode=1,
        lack_episode=1 if subscription_id % 2 == 0 else 2,
        note=[],
        state="R",
        sites=list(range(1, site_count + 1)),
        best_version=0,
    )


def _candidate_key(context: Context) -> str:
    """返回候选稳定资源键，用于集合与顺序对照。"""
    return str(context.torrent_info.enclosure if context.torrent_info else "")


def _baseline_route(
    candidates: dict[str, list[Context]],
    subscribe: SubscriptionSnapshot,
) -> list[str]:
    """用完整扫描实现受控显式身份样本的 canonical 参考集合。"""
    target = CandidateIndex.media_identity(subscribe)
    routed: list[str] = []
    for contexts in candidates.values():
        for context in contexts:
            if CandidateIndex.media_identity(context.media_info) != target:
                continue
            if not CandidateIndex.media_type_matches(context, subscribe):
                continue
            if not CandidateIndex.season_matches(context, subscribe):
                continue
            routed.append(_candidate_key(context))
    return routed


def _build_match_inputs(
    case: ScaleCase,
) -> tuple[dict[str, list[Context]], list[SubscriptionSnapshot]]:
    """为索引基线和实际 Match 构造彼此独立的固定输入快照。"""
    candidates = {
        f"site-{site_id}.example": []
        for site_id in range(1, case.site_count + 1)
    }
    for candidate_index in range(case.candidate_count):
        site_id = candidate_index % case.site_count + 1
        media_id = str(100000 + candidate_index % case.media_count)
        candidates[f"site-{site_id}.example"].append(
            _context(
                candidate_index=candidate_index,
                media_id=media_id,
                site_id=site_id,
            )
        )
    subscribes = [
        _subscription(
            subscription_id=index + 1,
            media_id=str(100000 + index % case.media_count),
            site_count=case.site_count,
        )
        for index in range(case.subscription_count)
    ]
    return candidates, subscribes


def _collection_digest(values: list[str]) -> str:
    """返回保留重复次数的稳定摘要，便于审计完整集合而不膨胀输出。"""
    payload = "\n".join(sorted(values)).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _compare_collections(expected: set[str], actual: list[str]) -> dict[str, Any]:
    """比较完整多重集合并保留计数、摘要、重复项和有限差异样本。"""
    expected_values = sorted(expected)
    actual_values = sorted(actual)
    expected_counts = Counter(expected_values)
    actual_counts = Counter(actual_values)
    missing = expected_counts - actual_counts
    unexpected = actual_counts - expected_counts
    return {
        "equal": actual_counts == expected_counts,
        "expected_count": len(expected_values),
        "actual_count": len(actual_values),
        "actual_distinct_count": len(actual_counts),
        "duplicate_count": sum(max(count - 1, 0) for count in actual_counts.values()),
        "expected_sha256": _collection_digest(expected_values),
        "actual_sha256": _collection_digest(actual_values),
        "missing_sample": sorted(missing.elements())[:5],
        "unexpected_sample": sorted(unexpected.elements())[:5],
    }


class _ScaleSubscriptionRepository:
    """为实际 Match 提供固定订阅列表和准入后的最新快照。"""

    def __init__(self, subscribes: list[SubscriptionSnapshot]) -> None:
        self._subscribes = list(subscribes)
        self._by_id = {subscribe.id: subscribe for subscribe in subscribes}

    def list(self, _state: str) -> list[SubscriptionSnapshot]:
        """返回固定顺序的完整订阅快照。"""
        return list(self._subscribes)

    def get(self, subscription_id: int) -> Optional[SubscriptionSnapshot]:
        """返回准入和提交前复读使用的当前订阅快照。"""
        return self._by_id.get(subscription_id)


class _ScaleSiteRepository:
    """把受控站点 ID 映射为候选分组域名。"""

    @staticmethod
    def get_domains_by_ids(site_ids: list[int]) -> list[str]:
        """返回固定快照内与站点 ID 一一对应的域名。"""
        return [f"site-{site_id}.example" for site_id in site_ids]


class _ScaleTorrentHelper:
    """隔离实际 Match 的种子属性过滤外部依赖。"""

    @staticmethod
    def filter_torrent(**_kwargs: Any) -> bool:
        """固定样本中的候选均通过订阅属性过滤。"""
        return True


_SITE_PRESSURE_SYNC_TIMEOUT = 5.0


class _ScaleSiteRequestBoundary:
    """记录确定性站点请求替身的在途数量和请求结果。"""

    def __init__(self, *, site_id: int) -> None:
        self.site_id = site_id
        self._lock = threading.Lock()
        self._active = 0
        self._peak = 0
        self._calls = 0
        self._outcomes: list[str] = []
        self._outcome = "success"
        self._error: Optional[str] = None
        self._active_at_rejection = 0
        self.first_started = threading.Event()
        self.round_observed = threading.Event()
        self.release_first = threading.Event()

    @property
    def active(self) -> int:
        """返回请求边界当前真实在途数量。"""
        with self._lock:
            return self._active

    @property
    def peak(self) -> int:
        """返回请求边界观测到的最大在途数量。"""
        with self._lock:
            return self._peak

    @property
    def calls(self) -> int:
        """返回请求边界实际进入次数。"""
        with self._lock:
            return self._calls

    @property
    def outcomes(self) -> tuple[str, ...]:
        """返回请求边界按进入顺序记录的结果分类。"""
        with self._lock:
            return tuple(self._outcomes)

    @property
    def active_at_rejection(self) -> int:
        """返回预算拒绝发生时请求边界的在途数量。"""
        with self._lock:
            return self._active_at_rejection

    def set_outcome(self, outcome: str, error: Optional[str] = None) -> None:
        """设置后续确定性请求要发布的站点观察结果。"""
        with self._lock:
            self._outcome = outcome
            self._error = error

    def record_budget_rejection(self) -> None:
        """记录 wrapper 拒绝时请求边界仍保持在途的事实。"""
        with self._lock:
            self._active_at_rejection = max(self._active_at_rejection, self._active)
        self.round_observed.set()

    def request(self, *, owner: str) -> list[str]:
        """在真实请求边界更新 active/peak 并返回固定站点结果。"""
        with self._lock:
            call_index = self._calls
            self._calls += 1
            self._active += 1
            self._peak = max(self._peak, self._active)
            outcome = self._outcome
            error = self._error
            self._outcomes.append(outcome)
            first_request = call_index == 0
            if call_index == 1:
                self.round_observed.set()
        if first_request:
            self.first_started.set()
            self.release_first.wait(timeout=_SITE_PRESSURE_SYNC_TIMEOUT)
        try:
            report_site_search_outcome(
                attempted=True,
                outcome=outcome,
                error=error,
            )
            if outcome == "rate_limited":
                return []
            return [f"site-{self.site_id}-{owner}-{call_index}"]
        finally:
            with self._lock:
                self._active -= 1


def _site_budget_record(
    engine: Any,
    site_id: int,
) -> Optional[SubscriptionSiteBudgetRecord]:
    """读取站点预算最终记录，核对 observation 是否实际持久化。"""
    with Session(engine) as session:
        return session.execute(
            select(SubscriptionSiteBudgetRecord).where(
                SubscriptionSiteBudgetRecord.site_id == site_id
            )
        ).scalars().first()


def _run_site_pressure_case(
    case: ScaleCase,
    search: TransactionalSubscriptionSearchRepository,
    engine: Any,
) -> dict[str, Any]:
    """让两个并发 SearchChain owner 通过 wrapper 验证每个站点的容量和冷却。"""
    site_observations: list[dict[str, Any]] = []
    duplicate_site_claims_blocked = 0
    error_cooldown_claims_blocked = 0
    successful_site_claims_reused = 0

    for site_id in range(1, case.site_count + 1):
        boundary = _ScaleSiteRequestBoundary(site_id=site_id)
        pressure_event = boundary.round_observed
        start_barrier = threading.Barrier(2)
        owners = ("scale-owner-a", "scale-owner-b")
        chains: dict[str, SearchChain] = {}
        owner_deferrals: dict[str, list[Any]] = {owner: [] for owner in owners}

        for owner in owners:
            controller = SubscriptionSiteBudgetController(
                repository=search,
                owner=owner,
                cancelled=lambda: False,
                stop_state=ProcessStopState(),
                lease_seconds=900,
            )
            chain = object.__new__(SearchChain)
            chain.configure_subscription_site_budget(controller)
            original_record_deferred = chain.record_subscription_site_budget_deferred

            def record_deferred(
                deferral: Any,
                *,
                original=original_record_deferred,
                owner_name=owner,
            ) -> None:
                """保留 wrapper 延后事实并同步压力轮次观测。"""
                original(deferral)
                owner_deferrals[owner_name].append(deferral)
                boundary.record_budget_rejection()
                pressure_event.set()

            chain.record_subscription_site_budget_deferred = record_deferred

            def search_site_torrents(*, _owner=owner, **_kwargs: Any) -> list[str]:
                """将 SearchChain 的真实站点请求委托给固定边界。"""
                return boundary.request(owner=_owner)

            chain.search_site_torrents = search_site_torrents
            chains[owner] = chain

        owner_results: dict[str, list[str]] = {}
        owner_errors: dict[str, str] = {}
        owner_invocations: dict[str, int] = {owner: 0 for owner in owners}
        result_lock = threading.Lock()

        def run_owner(owner: str) -> None:
            """同步启动一个 owner，并保存 wrapper 的结果或异常。"""
            try:
                start_barrier.wait(timeout=_SITE_PRESSURE_SYNC_TIMEOUT)
                with result_lock:
                    owner_invocations[owner] += 1
                result = chains[owner]._search_site_torrents_with_budget(  # pylint: disable=protected-access
                    site={"id": site_id, "name": f"Site {site_id}"},
                    keyword="governance-pressure",
                    mtype=None,
                    page=0,
                )
                with result_lock:
                    owner_results[owner] = result
            except Exception as error:  # noqa: BLE001
                with result_lock:
                    owner_errors[owner] = str(error)

        threads = [
            threading.Thread(target=run_owner, args=(owner,), daemon=True)
            for owner in owners
        ]
        for thread in threads:
            thread.start()
        first_request_started = boundary.first_started.wait(
            timeout=_SITE_PRESSURE_SYNC_TIMEOUT,
        )
        contention_observed = pressure_event.wait(timeout=_SITE_PRESSURE_SYNC_TIMEOUT)
        boundary.release_first.set()
        for thread in threads:
            thread.join(timeout=_SITE_PRESSURE_SYNC_TIMEOUT)
        owners_finished = all(not thread.is_alive() for thread in threads)

        initial_deferrals = [
            deferral
            for owner in owners
            for deferral in owner_deferrals[owner]
        ]
        successful_owners = [
            owner
            for owner in owners
            if owner in owner_results and owner_results[owner]
        ]
        blocked_owners = [
            owner
            for owner in owners
            if owner in owner_results
            and not owner_results[owner]
            and owner_deferrals[owner]
        ]
        successful_owner = successful_owners[0] if len(successful_owners) == 1 else None
        blocked_owner = blocked_owners[0] if len(blocked_owners) == 1 else None
        initial_pressure_valid = bool(
            first_request_started
            and contention_observed
            and boundary.active_at_rejection == 1
            and boundary.peak == 1
            and boundary.active == 0
            and len(initial_deferrals) == 1
            and len(successful_owners) == 1
            and len(blocked_owners) == 1
            and all(count == 1 for count in owner_invocations.values())
            and owners_finished
            and set(owner_results) == set(owners)
            and not owner_errors
        )

        success_reused = False
        error_observed = False
        error_cooldown_blocked = False
        error_cooldown_persisted = False
        reuse_calls = 0
        error_calls = 0
        cooldown_calls = 0
        if successful_owner and blocked_owner:
            winner = chains[successful_owner]
            loser = chains[blocked_owner]
            winner.consume_subscription_site_budget_deferrals()
            loser.consume_subscription_site_budget_deferrals()

            if site_id != case.site_count:
                boundary.set_outcome("success")
                calls_before_reuse = boundary.calls
                reused = winner._search_site_torrents_with_budget(  # pylint: disable=protected-access
                    site={"id": site_id, "name": f"Site {site_id}"},
                    keyword="governance-pressure-reuse",
                    mtype=None,
                    page=0,
                )
                reuse_calls = boundary.calls - calls_before_reuse
                reuse_record = _site_budget_record(engine, site_id)
                success_reused = bool(
                    reused
                    and reuse_calls == 1
                    and boundary.active == 0
                    and reuse_record
                    and reuse_record.last_outcome == "success"
                    and reuse_record.lease_token is None
                )
                winner.consume_subscription_site_budget_failures()

            if site_id == case.site_count:
                boundary.set_outcome("rate_limited", error="HTTP 429")
                calls_before_error = boundary.calls
                error_result = winner._search_site_torrents_with_budget(  # pylint: disable=protected-access
                    site={"id": site_id, "name": f"Site {site_id}"},
                    keyword="governance-pressure-error",
                    mtype=None,
                    page=0,
                )
                error_calls = boundary.calls - calls_before_error
                error_record = _site_budget_record(engine, site_id)
                error_observed = bool(
                    not error_result
                    and error_calls == 1
                    and boundary.active == 0
                    and boundary.outcomes[-1:] == ("rate_limited",)
                )
                winner.consume_subscription_site_budget_failures()

                calls_before_cooldown = boundary.calls
                cooldown_result = loser._search_site_torrents_with_budget(  # pylint: disable=protected-access
                    site={"id": site_id, "name": f"Site {site_id}"},
                    keyword="governance-pressure-cooldown",
                    mtype=None,
                    page=0,
                )
                cooldown_calls = boundary.calls - calls_before_cooldown
                cooldown_deferrals = loser.consume_subscription_site_budget_deferrals()
                error_cooldown_blocked = bool(
                    not cooldown_result
                    and cooldown_calls == 0
                    and cooldown_deferrals
                )
                error_cooldown_persisted = bool(
                    error_record
                    and error_record.last_outcome == "rate_limited"
                    and error_record.lease_token is None
                    and error_record.next_allowed_at
                    > datetime.now(timezone.utc).isoformat(timespec="seconds")
                )

        site_pressure_valid = bool(
            initial_pressure_valid
            and (
                success_reused
                if site_id != case.site_count
                else error_observed
                and error_cooldown_blocked
                and error_cooldown_persisted
            )
        )
        duplicate_site_claims_blocked += len(initial_deferrals)
        successful_site_claims_reused += int(success_reused)
        error_cooldown_claims_blocked += int(error_cooldown_blocked)
        site_observations.append(
            {
                "site_id": site_id,
                "owner_count": len(owners),
                "owner_invocations": sum(owner_invocations.values()),
                "owners_finished": owners_finished,
                "request_active": boundary.active,
                "request_peak": boundary.peak,
                "request_calls": boundary.calls,
                "request_active_at_rejection": boundary.active_at_rejection,
                "budget_rejections": len(initial_deferrals),
                "success_reused": success_reused,
                "error_observed": error_observed,
                "error_cooldown_blocked": error_cooldown_blocked,
                "error_cooldown_persisted": error_cooldown_persisted,
                "reuse_calls": reuse_calls,
                "error_calls": error_calls,
                "cooldown_calls": cooldown_calls,
                "pressure_valid": site_pressure_valid,
            }
        )

    peaks = [item["request_peak"] for item in site_observations]
    return {
        "site_peak_inflight_per_site": max(peaks, default=0),
        "site_request_boundary_active": max(
            (item["request_active"] for item in site_observations),
            default=0,
        ),
        "site_request_boundary_peak": max(peaks, default=0),
        "site_request_boundary_calls": sum(
            item["request_calls"] for item in site_observations
        ),
        "site_pressure_owner_count": 2,
        "site_pressure_concurrency_verified": all(
            item["owner_count"] == 2
            and item["owner_invocations"] == 2
            and item["owners_finished"]
            and item["request_active_at_rejection"] == 1
            for item in site_observations
        ),
        "site_pressure_success_release_reused": successful_site_claims_reused
        == max(0, case.site_count - 1),
        "site_pressure_error_observation_cooled": (
            error_cooldown_claims_blocked == 1
            and bool(site_observations)
            and site_observations[-1]["error_observed"]
            and site_observations[-1]["error_cooldown_persisted"]
        ),
        "site_pressure_valid": all(
            item["pressure_valid"] for item in site_observations
        ),
        "site_observations": site_observations,
        "duplicate_site_claims_blocked": duplicate_site_claims_blocked,
        "error_cooldown_claims_blocked": error_cooldown_claims_blocked,
        "successful_site_claims_reused": successful_site_claims_reused,
    }


class _ScaleDownloadBoundary(DownloadChain):
    """运行 canonical 批量选择，并在实际下载器边界返回确定性成功。"""

    def __init__(self, matched: list[str], downloaded: list[str]) -> None:
        self._matched = matched
        self._downloaded = downloaded
        self._subscription_id = 0

    @staticmethod
    def _prepare_batch_download_contexts(
        *,
        contexts: list[Context],
        **_kwargs: Any,
    ) -> tuple[list[Context], dict[str, Any]]:
        """固定快照保持候选顺序且不读取历史失败冷却。"""
        return contexts, {}

    def batch_download(
        self,
        *,
        contexts: list[Context],
        no_exists: dict[Any, dict[int, NotExistMediaInfo]],
        source: str,
        governance: Any,
        **_kwargs: Any,
    ) -> tuple[list[Context], dict[Any, dict[int, NotExistMediaInfo]]]:
        """记录 Match 提交集合后运行真实批量候选选择。"""
        source_data = json.loads(source.removeprefix("Subscribe|"))
        self._subscription_id = int(source_data["id"])
        for context in contexts:
            self._matched.append(f"{self._subscription_id}|{_candidate_key(context)}")
        return super().batch_download(
            contexts=contexts,
            no_exists=no_exists,
            source=source,
            governance=governance,
            **_kwargs,
        )

    def download_single(
        self,
        context: Context,
        *,
        governance: Any,
        **_kwargs: Any,
    ) -> str:
        """模拟下载器明确成功，并记录 canonical 选择出的资源。"""
        if governance.cancelled and governance.cancelled():
            raise AssertionError("固定快照执行不应在下载前取消")
        if governance.mark_started:
            governance.mark_started()
        self._downloaded.append(f"{self._subscription_id}|{_candidate_key(context)}")
        return f"scale-download-{self._subscription_id}"


def _run_match_case(case: ScaleCase) -> dict[str, Any]:
    """比较完整扫描与无损索引，并记录单轮事实租约和本地等待分位。"""
    candidates, subscribes = _build_match_inputs(case)

    index = CandidateIndex(candidates)
    baseline_comparisons = case.subscription_count * case.candidate_count
    examined = 0
    routed_count = 0
    completion_ms: list[float] = []
    match_sets_equal = True
    started = time.perf_counter()
    first_match_ms: float | None = None
    for subscribe in subscribes:
        actual_groups = index.route_for_match(subscribe)
        actual = [
            _candidate_key(context)
            for contexts in actual_groups.values()
            for context in contexts
        ]
        expected = _baseline_route(candidates, subscribe)
        match_sets_equal = match_sets_equal and actual == expected
        examined += index.last_examined_count
        routed_count += len(actual)
        elapsed_ms = (time.perf_counter() - started) * 1000
        completion_ms.append(elapsed_ms)
        if actual and first_match_ms is None:
            first_match_ms = elapsed_ms

    lease = FreshFactLease()
    for subscribe in subscribes:
        lease.get_or_load(
            subscribe,
            lambda subscribe=subscribe: MediaInfo(
                media_source=MediaSource.TMDB,
                media_id=subscribe.media_id,
                type=MediaType.TV,
                season=1,
            ),
        )

    return {
        **asdict(case),
        "baseline_comparisons": baseline_comparisons,
        "candidate_checks": examined,
        "candidate_check_reduction_percent": round(
            (1 - examined / baseline_comparisons) * 100,
            3,
        ),
        "routed_candidates": routed_count,
        "match_sets_equal": match_sets_equal,
        "fact_loads": lease.loads,
        "fact_hits": lease.hits,
        "first_match_local_ms": round(first_match_ms or 0.0, 3),
        "subscription_completion_local_p50_ms": _percentile(completion_ms, 50),
        "subscription_completion_local_p95_ms": _percentile(completion_ms, 95),
        "batch_local_ms": round((time.perf_counter() - started) * 1000, 3),
    }


def _run_match_execution_case(case: ScaleCase) -> dict[str, Any]:
    """运行实际 SubscribeChain Match 并对照四类完整业务集合。"""
    candidates, subscribes = _build_match_inputs(case)
    expected_matched: set[str] = set()
    expected_downloaded: set[str] = set()
    expected_missing = {
        f"{subscribe.id}|2"
        for subscribe in subscribes
        if subscribe.id % 2 == 1
    }
    expected_completed = {
        str(subscribe.id)
        for subscribe in subscribes
        if subscribe.id % 2 == 0
    }
    for subscribe in subscribes:
        routed = _baseline_route(candidates, subscribe)
        expected_matched.update(
            f"{subscribe.id}|{candidate_key}"
            for candidate_key in routed
        )
        expected_downloaded.add(f"{subscribe.id}|{routed[0]}")

    actual_matched: list[str] = []
    actual_downloaded: list[str] = []
    actual_missing: list[str] = []
    actual_completed: list[str] = []
    settlement_ms: list[float] = []
    recognition_calls = 0
    progress_snapshots: list[dict[str, int]] = []
    info_logs: list[str] = []
    started = time.perf_counter()

    class _ScaleMatchLogger:
        """只收集 Match 模块 INFO，用于验证日志量不随候选乘积增长。"""

        @staticmethod
        def debug(*_args: Any, **_kwargs: Any) -> None:
            """忽略受控样本的逐项诊断日志。"""

        @staticmethod
        def warning(*_args: Any, **_kwargs: Any) -> None:
            """忽略受控样本中预期不会出现的警告。"""

        warn = warning

        @staticmethod
        def error(*_args: Any, **_kwargs: Any) -> None:
            """错误仍由集合和终态门禁暴露，不污染 INFO 计数。"""

        @staticmethod
        def info(message: Any, *_args: Any, **_kwargs: Any) -> None:
            """保存结构化批次摘要。"""
            info_logs.append(str(message))

    class _ScaleMediaChain:
        """用固定新鲜媒体事实替代目标规模下的真实 TMDB 请求。"""

        def recognize_media(self, **kwargs: Any) -> MediaInfo:
            """按订阅身份返回两集电视剧的新鲜事实。"""
            nonlocal recognition_calls
            recognition_calls += 1
            media_id = str(kwargs["media_id"])
            return MediaInfo(
                media_source=kwargs["media_source"],
                media_id=media_id,
                type=MediaType.TV,
                title=f"Governance Show {media_id}",
                year="2026",
                season=1,
                seasons={1: [1, 2]},
            )

        @staticmethod
        def recognize_by_meta(*_args: Any, **_kwargs: Any) -> MediaInfo:
            """明确身份候选不应进入二次识别。"""
            raise AssertionError("明确身份候选不应重新识别")

        @staticmethod
        def supplement_media_info(mediainfo: MediaInfo) -> MediaInfo:
            """固定样本无需访问其他元数据来源。"""
            return mediainfo

    chain = object.__new__(SubscribeChain)
    chain.subscription_repository = _ScaleSubscriptionRepository(subscribes)
    chain.site_repository = _ScaleSiteRepository()
    chain._match_lock = threading.Lock()
    chain._search_queue_lock = threading.Lock()
    chain._subscription_execution_admission = SubscriptionExecutionAdmission()
    chain.get_sub_sites = lambda subscribe: list(subscribe.sites or [])
    chain.get_params = lambda _subscribe: {}
    chain.filter_torrents = lambda *, torrent_list, **_kwargs: torrent_list

    initial_missing = {
        subscribe.id: [1] if subscribe.id % 2 == 0 else [1, 2]
        for subscribe in subscribes
    }

    def check_existing(
        *,
        subscribe: SubscriptionSnapshot,
        mediakey: Any,
        **_kwargs: Any,
    ) -> tuple[bool, dict[Any, dict[int, NotExistMediaInfo]]]:
        """返回每条订阅固定的初始缺集快照。"""
        return False, {
            mediakey: {
                1: NotExistMediaInfo(
                    season=1,
                    episodes=list(initial_missing[subscribe.id]),
                    total_episode=2,
                    start_episode=1,
                )
            }
        }

    def record_download_facts(
        subscribe: SubscriptionSnapshot,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        """让完成判断消费下载边界结果，不引入持久化测试替身。"""
        return {
            "episodes": [1],
            "fields": [],
            "updated": False,
            "subscribe": subscribe,
        }

    def record_missing(
        *,
        no_exists: dict[Any, dict[int, NotExistMediaInfo]],
        subscribe: SubscriptionSnapshot,
        **_kwargs: Any,
    ) -> None:
        """记录实际完成判断收到的剩余缺集集合。"""
        for seasons in no_exists.values():
            for missing in seasons.values():
                actual_missing.extend(
                    f"{subscribe.id}|{episode}"
                    for episode in missing.episodes or []
                )

    chain.check_and_handle_existing_media = check_existing
    chain._SubscribeChain__record_subscribe_download_facts = record_download_facts
    chain._SubscribeChain__refresh_subscribe_progress_with_no_exists = record_missing
    chain._SubscribeChain__finish_subscribe = (
        lambda *, subscribe, **_kwargs: actual_completed.append(str(subscribe.id))
    )
    original_finish = chain.finish_subscribe_or_not

    def finish_and_record(**kwargs: Any) -> None:
        """运行 canonical 完成判断并记录每条订阅收口时间。"""
        original_finish(**kwargs)
        settlement_ms.append((time.perf_counter() - started) * 1000)

    chain.finish_subscribe_or_not = finish_and_record
    download_boundary = _ScaleDownloadBoundary(actual_matched, actual_downloaded)

    def record_progress(**kwargs: Any) -> None:
        """保存 Match 对外发布的真实批次聚合状态。"""
        if kwargs.get("data"):
            progress_snapshots.append(dict(kwargs["data"]))

    with (
        patch.object(subscribe_match, "MediaChain", _ScaleMediaChain),
        patch.object(subscribe_match, "TorrentHelper", _ScaleTorrentHelper),
        patch.object(
            subscribe_match,
            "get_configured_system_config",
            return_value=SimpleNamespace(get=lambda _key: []),
        ),
        patch.object(
            subscribe_match,
            "runtime_stop_state",
            SimpleNamespace(is_system_stopped=False),
        ),
        patch.object(subscribe_match, "logger", _ScaleMatchLogger()),
        patch.object(
            download_batch,
            "runtime_stop_state",
            SimpleNamespace(is_system_stopped=False),
        ),
        patch.object(
            subscribe_policy,
            "DownloadChain",
            return_value=download_boundary,
        ),
    ):
        chain.match(candidates, progress_callback=record_progress)

    final_progress = progress_snapshots[-1]
    return {
        "name": case.name,
        "subscription_count": case.subscription_count,
        "site_count": case.site_count,
        "candidate_count": case.candidate_count,
        "matched_candidates": _compare_collections(expected_matched, actual_matched),
        "downloaded_candidates": _compare_collections(expected_downloaded, actual_downloaded),
        "remaining_missing_episodes": _compare_collections(expected_missing, actual_missing),
        "completed_subscriptions": _compare_collections(expected_completed, actual_completed),
        "fresh_fact_loads": recognition_calls,
        "match_execution_completed": final_progress == {
            "total": case.subscription_count,
            "finished": case.subscription_count,
            "completed": case.subscription_count,
            "skipped": 0,
            "failed": 0,
        },
        "info_log_count": len(info_logs),
        "info_log_bounded": bool(
            len(info_logs) == 2
            and info_logs[0].startswith("订阅治理轮次开始: operation=match ")
            and info_logs[1].startswith("订阅治理轮次结束: operation=match ")
        ),
        "first_settlement_local_ms": round(settlement_ms[0], 3),
        "subscription_settlement_local_p50_ms": _percentile(settlement_ms, 50),
        "subscription_settlement_local_p95_ms": _percentile(settlement_ms, 95),
        "batch_local_ms": round((time.perf_counter() - started) * 1000, 3),
    }


def _run_durable_governance(case: ScaleCase, workdir: Path) -> dict[str, Any]:
    """验证持久队列、站点预算、恢复、取消与订阅级准入。"""
    engine = create_engine(
        f"sqlite:///{workdir / (case.name + '.db')}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    search = TransactionalSubscriptionSearchRepository(factory)

    started = time.perf_counter()
    enqueued = search.enqueue(
        subscription_ids=tuple(range(1, case.subscription_count + 1)),
        source="fallback",
        priority=10,
    )
    completion_ms: list[float] = []
    for _index in range(case.subscription_count):
        task = search.claim_next(owner="acceptance-worker")
        if task is None or not task.lease_token:
            raise AssertionError("受控队列未返回预期任务租约")
        if not search.update_task_phase(
            task_id=task.task_id,
            lease_token=task.lease_token,
            phase="searching",
            current_site_id=(_index % case.site_count) + 1,
        ):
            raise AssertionError("当前租约无法更新任务阶段")
        if not search.finish_task(
            task_id=task.task_id,
            lease_token=task.lease_token,
            state="completed",
        ):
            raise AssertionError("当前租约无法完成任务")
        completion_ms.append((time.perf_counter() - started) * 1000)
    completed_batch = search.get_batch(enqueued.batch.batch_id)

    recovery = search.enqueue(
        subscription_ids=(case.subscription_count + 1,),
        source="targeted",
        priority=80,
    )
    first_claim = search.claim_next(owner="crashed-worker")
    if first_claim is None:
        raise AssertionError("恢复样本未取得初始任务")
    expired_at = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(timespec="seconds")
    with Session(engine) as session:
        session.execute(
            update(SubscriptionSearchTask)
            .where(SubscriptionSearchTask.task_id == first_claim.task_id)
            .values(lease_expires_at=expired_at)
        )
        session.commit()
    recovered_claim = search.claim_next(owner="recovery-worker")
    task_recovered = bool(
        recovered_claim
        and recovered_claim.task_id == first_claim.task_id
        and recovered_claim.lease_token != first_claim.lease_token
        and recovered_claim.attempt_count == 2
    )
    if recovered_claim and recovered_claim.lease_token:
        search.finish_task(
            task_id=recovered_claim.task_id,
            lease_token=recovered_claim.lease_token,
            state="completed",
        )

    cancellation = search.enqueue(
        subscription_ids=tuple(
            range(case.subscription_count + 10, case.subscription_count + 20)
        ),
        source="manual",
        priority=100,
    )
    running = search.claim_next(owner="cancel-worker")
    if running is None or not running.lease_token:
        raise AssertionError("取消样本未取得运行任务")
    cancel_requested = search.request_cancel(cancellation.batch.batch_id)
    running_cancelled = search.release_task(
        task_id=running.task_id,
        lease_token=running.lease_token,
        cancelled=True,
    )
    cancelled_batch = search.get_batch(cancellation.batch.batch_id)

    site_pressure = _run_site_pressure_case(case, search, engine)

    recovery_site = case.site_count + 1
    site_first = search.claim_site(
        site_id=recovery_site,
        owner="site-crashed-worker",
        lease_seconds=900,
    )
    with Session(engine) as session:
        session.execute(
            update(SubscriptionSiteBudgetRecord)
            .where(SubscriptionSiteBudgetRecord.site_id == recovery_site)
            .values(lease_expires_at=expired_at)
        )
        session.commit()
    site_recovered_claim = search.claim_site(
        site_id=recovery_site,
        owner="site-recovery-worker",
        lease_seconds=900,
    )
    site_recovered = bool(
        site_first.acquired
        and site_recovered_claim.acquired
        and site_recovered_claim.lease_token != site_first.lease_token
    )

    admission = SubscriptionExecutionAdmission()
    leases = [
        admission.try_acquire(
            subscription_id=subscription_id,
            operation="search",
            ttl_seconds=60,
        )
        for subscription_id in range(1, case.subscription_count + 1)
    ]
    acquired_leases = [lease for lease in leases if lease is not None]
    same_subscription_conflicts_blocked = sum(
        admission.try_acquire(
            subscription_id=subscription_id,
            operation="match",
            ttl_seconds=60,
        ) is None
        for subscription_id in range(1, case.subscription_count + 1)
    )
    initial_release_count = sum(admission.release(lease) for lease in acquired_leases)
    reacquired_leases = [
        admission.try_acquire(
            subscription_id=subscription_id,
            operation="match",
            ttl_seconds=60,
        )
        for subscription_id in range(1, case.subscription_count + 1)
    ]
    reacquired_count = sum(lease is not None for lease in reacquired_leases)
    final_release_count = sum(
        admission.release(lease)
        for lease in reacquired_leases
        if lease is not None
    )

    return {
        "name": case.name,
        "queue_created_count": enqueued.created_count,
        "queue_state": completed_batch.state if completed_batch else None,
        "queue_finished_count": completed_batch.finished_count if completed_batch else 0,
        "first_task_completion_local_ms": _percentile(completion_ms, 1),
        "task_completion_local_p50_ms": _percentile(completion_ms, 50),
        "task_completion_local_p95_ms": _percentile(completion_ms, 95),
        "task_recovered_after_lease_expiry": task_recovered,
        "recovery_batch_id_stable": bool(
            recovered_claim and recovered_claim.batch_id == recovery.batch.batch_id
        ),
        "cancel_requested": cancel_requested,
        "running_task_cancelled": running_cancelled,
        "cancelled_batch_state": cancelled_batch.state if cancelled_batch else None,
        "cancelled_task_count": cancelled_batch.cancelled_count if cancelled_batch else 0,
        "site_count": case.site_count,
        **site_pressure,
        "site_recovered_after_lease_expiry": site_recovered,
        "subscription_leases_acquired": len(acquired_leases),
        "same_subscription_conflicts_blocked": same_subscription_conflicts_blocked,
        "subscription_leases_released": initial_release_count,
        "subscription_leases_reacquired": reacquired_count,
        "reacquired_subscription_leases_released": final_release_count,
    }


def run_acceptance() -> dict[str, Any]:
    """运行两档最终矩阵并返回全部指标与门禁结论。"""
    cases = (
        ScaleCase("medium", 100, 10, 1000, 100),
        ScaleCase("large", 200, 20, 1200, 100),
    )
    match_cases = [_run_match_case(case) for case in cases]
    match_execution_cases = [_run_match_execution_case(case) for case in cases]
    with tempfile.TemporaryDirectory(prefix="subscription-governance-") as directory:
        durable_cases = [
            _run_durable_governance(case, Path(directory))
            for case in cases
        ]

    gates = {
        "match_sets_equal": all(case["match_sets_equal"] for case in match_cases),
        "candidate_product_avoided": all(
            case["candidate_checks"] < case["baseline_comparisons"] / 20
            for case in match_cases
        ),
        "fresh_facts_bounded_by_unique_media": all(
            case["fact_loads"] == case["media_count"]
            for case in match_cases
        ),
        "match_execution_candidate_sets_equal": all(
            case["matched_candidates"]["equal"]
            for case in match_execution_cases
        ),
        "match_execution_download_sets_equal": all(
            case["downloaded_candidates"]["equal"]
            for case in match_execution_cases
        ),
        "match_execution_missing_sets_equal": all(
            case["remaining_missing_episodes"]["equal"]
            for case in match_execution_cases
        ),
        "match_execution_completion_sets_equal": all(
            case["completed_subscriptions"]["equal"]
            for case in match_execution_cases
        ),
        "match_execution_batches_completed": all(
            case["match_execution_completed"]
            for case in match_execution_cases
        ),
        "match_execution_fresh_facts_bounded": all(
            case["fresh_fact_loads"] == match_case["media_count"]
            for case, match_case in zip(match_execution_cases, match_cases)
        ),
        "match_info_logs_bounded": all(
            case["info_log_bounded"]
            for case in match_execution_cases
        ),
        "queue_completed": all(
            case["queue_state"] == "completed"
            and case["queue_finished_count"] == next(
                item.subscription_count for item in cases if item.name == case["name"]
            )
            for case in durable_cases
        ),
        "task_and_site_recovery": all(
            case["task_recovered_after_lease_expiry"]
            and case["site_recovered_after_lease_expiry"]
            for case in durable_cases
        ),
        "cancellation_terminal": all(
            case["cancel_requested"]
            and case["running_task_cancelled"]
            and case["cancelled_batch_state"] == "cancelled"
            and case["cancelled_task_count"] == 10
            for case in durable_cases
        ),
        "site_pressure_bounded": all(
            case["site_peak_inflight_per_site"] == 1
            and case["site_request_boundary_peak"] == 1
            and case["site_request_boundary_active"] == 0
            and case["duplicate_site_claims_blocked"] == case["site_count"]
            and case["error_cooldown_claims_blocked"] == 1
            and case["successful_site_claims_reused"] == case["site_count"] - 1
            and case["site_pressure_concurrency_verified"]
            and case["site_pressure_success_release_reused"]
            and case["site_pressure_error_observation_cooled"]
            and case["site_pressure_valid"]
            for case in durable_cases
        ),
        "subscription_admission_serializes": all(
            case["subscription_leases_acquired"]
            == next(item.subscription_count for item in cases if item.name == case["name"])
            and case["same_subscription_conflicts_blocked"]
            == next(item.subscription_count for item in cases if item.name == case["name"])
            and case["subscription_leases_released"]
            == next(item.subscription_count for item in cases if item.name == case["name"])
            and case["subscription_leases_reacquired"]
            == next(item.subscription_count for item in cases if item.name == case["name"])
            and case["reacquired_subscription_leases_released"]
            == next(item.subscription_count for item in cases if item.name == case["name"])
            for case in durable_cases
        ),
    }
    return {
        "schema_version": 5,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "revision": _git_revision(),
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "database": "SQLite isolated temporary files",
            "external_requests": 0,
            "external_downloads": 0,
        },
        "method": {
            "sample": "Deterministic local controlled snapshot",
            "match_entrypoint": "SubscribeChain.match",
            "download_selection": "DownloadChain.batch_download",
            "fixed_external_boundaries": [
                "TMDB",
                "media_server",
                "downloader",
            ],
            "fixed_persistence_boundaries": [
                "subscription_repository",
                "download_facts",
                "subscription_progress",
                "completion_side_effects",
            ],
            "fixed_policy_inputs": [
                "site_mapping",
                "system_configuration",
                "subscription_filters",
                "torrent_attribute_filter",
                "download_preparation",
            ],
            "latency_scope": "in-process Match and isolated SQLite queue work",
            "production_network_slo": False,
        },
        "match_cases": match_cases,
        "match_execution_cases": match_execution_cases,
        "durable_cases": durable_cases,
        "gates": gates,
        "passed": all(gates.values()),
    }


def main() -> int:
    """解析输出路径，运行门禁并以进程状态反映验收结果。"""
    parser = argparse.ArgumentParser(
        description=(
            "运行 100/10/1000 与 200/20/1200 两档离线订阅治理验收；"
            "结果仅代表固定输入下的本地 Match、下载选择和隔离 SQLite，"
            "不代表生产网络 SLO。"
        )
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = run_acceptance()
    payload = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    else:
        sys.stdout.write(payload)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
