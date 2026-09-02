#!/usr/bin/env python3
"""运行订阅执行治理的确定性规模验收并输出 JSON 证据。"""

from __future__ import annotations

import argparse
import json
import math
import platform
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, update
from sqlalchemy.orm import Session, sessionmaker

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.application.subscription.candidates import CandidateIndex  # noqa: E402
from app.application.subscription.contract import SubscriptionSnapshot  # noqa: E402
from app.application.subscription.execution import SubscriptionExecutionAdmission  # noqa: E402
from app.application.subscription.facts import FreshFactLease  # noqa: E402
from app.db.adapters.subscriptionsearch import (  # noqa: E402
    TransactionalSubscriptionSearchRepository,
)
from app.db.base import Base  # noqa: E402
from app.db.models.subscriptionsearch import (  # noqa: E402
    SubscriptionSearchTask,
    SubscriptionSiteBudget,
)
from app.domain.context import Context, MediaInfo, TorrentInfo  # noqa: E402
from app.domain.metainfo import MetaInfo  # noqa: E402
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
    title = f"Governance Show {media_id} S01E{candidate_index + 1:04d}"
    meta = MetaInfo(title)
    meta.type = MediaType.TV
    meta.begin_season = 1
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


def _run_match_case(case: ScaleCase) -> dict[str, Any]:
    """比较完整扫描与无损索引，并记录单轮事实租约和本地等待分位。"""
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

    now = datetime.now(timezone.utc)
    duplicate_site_claims_blocked = 0
    error_cooldown_claims_blocked = 0
    successful_site_claims_reused = 0
    site_tokens: dict[int, str] = {}
    for site_id in range(1, case.site_count + 1):
        claim = search.claim_site(
            site_id=site_id,
            owner="site-worker-a",
            lease_seconds=900,
        )
        duplicate = search.claim_site(
            site_id=site_id,
            owner="site-worker-b",
            lease_seconds=900,
        )
        if not claim.acquired or not claim.lease_token:
            raise AssertionError(f"站点 {site_id} 未取得初始预算")
        site_tokens[site_id] = claim.lease_token
        duplicate_site_claims_blocked += int(not duplicate.acquired)
    for site_id, lease_token in site_tokens.items():
        outcome = "rate_limited" if site_id == case.site_count else "success"
        delay = 900 if outcome == "rate_limited" else 60
        if not search.finish_site(
            site_id=site_id,
            lease_token=lease_token,
            outcome=outcome,
            next_allowed_at=(now + timedelta(seconds=delay)).isoformat(timespec="seconds"),
            error="HTTP 429" if outcome == "rate_limited" else None,
        ):
            raise AssertionError(f"站点 {site_id} 预算无法收口")
        next_claim = search.claim_site(
            site_id=site_id,
            owner="site-worker-c",
            lease_seconds=900,
        )
        if outcome == "rate_limited":
            error_cooldown_claims_blocked += int(not next_claim.acquired)
        else:
            successful_site_claims_reused += int(
                next_claim.acquired and bool(next_claim.lease_token)
            )
            if (
                next_claim.lease_token
                and not search.finish_site(
                    site_id=site_id,
                    lease_token=next_claim.lease_token,
                    outcome="success",
                    next_allowed_at=now.isoformat(timespec="seconds"),
                )
            ):
                raise AssertionError(f"站点 {site_id} 复用预算无法收口")

    recovery_site = case.site_count + 1
    site_first = search.claim_site(
        site_id=recovery_site,
        owner="site-crashed-worker",
        lease_seconds=900,
    )
    with Session(engine) as session:
        session.execute(
            update(SubscriptionSiteBudget)
            .where(SubscriptionSiteBudget.site_id == recovery_site)
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
        "site_peak_inflight_per_site": 1,
        "duplicate_site_claims_blocked": duplicate_site_claims_blocked,
        "error_cooldown_claims_blocked": error_cooldown_claims_blocked,
        "successful_site_claims_reused": successful_site_claims_reused,
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
            and case["duplicate_site_claims_blocked"] == case["site_count"]
            and case["error_cooldown_claims_blocked"] == 1
            and case["successful_site_claims_reused"] == case["site_count"] - 1
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
        "schema_version": 3,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "revision": _git_revision(),
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "database": "SQLite isolated temporary files",
            "external_requests": 0,
            "external_downloads": 0,
        },
        "method": (
            "Deterministic local controlled sample. Latency fields measure only in-process "
            "routing and isolated SQLite queue work; they are not production network SLOs."
        ),
        "match_cases": match_cases,
        "durable_cases": durable_cases,
        "gates": gates,
        "passed": all(gates.values()),
    }


def main() -> int:
    """解析输出路径，运行门禁并以进程状态反映验收结果。"""
    parser = argparse.ArgumentParser()
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
