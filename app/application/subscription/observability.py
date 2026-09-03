"""订阅治理轮次日志的聚合与格式化。"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Collection, Literal, Mapping, Optional
from uuid import uuid4

from app.application.subscription.execution import (
    SearchBatchSnapshot,
    SubscriptionExecutionContext,
    SubscriptionSearchRepository,
)
from app.application.subscription.sitebudget import SubscriptionSiteBudgetMetrics

SearchTaskOutcome = Literal["completed", "skipped", "failed", "cancelled", "requeued"]


def batch_progress_text(batch: Optional[SearchBatchSnapshot]) -> str:
    """把批次聚合终态转为兼容进度文案。"""
    if batch is None:
        return "订阅搜索任务已提交"
    if batch.state == "failed":
        return "订阅搜索完成，部分任务失败"
    if batch.state == "cancelled":
        return "订阅搜索已取消"
    if batch.state == "skipped":
        return "订阅搜索完成，部分任务本轮已跳过"
    if batch.state in {"queued", "running", "cancelling"}:
        return "订阅搜索任务已排队"
    if batch.skipped_count:
        return "订阅搜索完成，部分任务本轮已跳过"
    return "订阅搜索完成"


def inline_search_result(total: int, finished: int) -> tuple[str, dict[str, int]]:
    """返回兼容搜索的真实终态文案与计数。"""
    text = (
        "订阅搜索完成"
        if finished == total
        else "订阅搜索结束，部分订阅本轮未执行或未完成"
    )
    return text, {"total": total, "finished": finished}


def batch_finished_count(
    batch: Optional[SearchBatchSnapshot],
    fallback: int,
) -> int:
    """返回批次所有终态任务数；批次暂不可读时使用本轮实际完成数。"""
    if batch is None:
        return fallback
    return (
        batch.finished_count
        + batch.failed_count
        + batch.cancelled_count
        + batch.skipped_count
    )


def finish_returned_search_task(
    *,
    queue: SubscriptionSearchRepository,
    task_id: str,
    lease_token: str,
    subscription_id: int,
    execution_context: SubscriptionExecutionContext,
    system_stopped: bool,
    cancel_requested: bool,
) -> tuple[Optional[int], SearchTaskOutcome, Optional[str]]:
    """按 TTL、停机和取消边界收口正常返回的搜索任务。"""
    download_started = execution_context.download_started
    if execution_context.is_expired() and not (system_stopped or cancel_requested):
        if download_started:
            queue.finish_task(
                task_id=task_id,
                lease_token=lease_token,
                state="completed",
                error="执行截止时间晚于下载提交边界，已按实际结果完成",
            )
            return subscription_id, "completed", "ttl_timeout"
        queue.finish_task(
            task_id=task_id,
            lease_token=lease_token,
            state="failed",
            error="订阅执行已超过协作截止时间",
        )
        return None, "failed", "ttl_timeout"
    if system_stopped:
        if download_started:
            queue.finish_task(
                task_id=task_id,
                lease_token=lease_token,
                state="completed",
                error="停机请求晚于下载提交边界，已按实际结果完成",
            )
            return subscription_id, "completed", "system_stop"
        queue.release_task(task_id=task_id, lease_token=lease_token)
        return None, "requeued", "system_stop"
    if cancel_requested:
        if download_started:
            queue.finish_task(
                task_id=task_id,
                lease_token=lease_token,
                state="completed",
                error="取消请求晚于下载提交边界，已按实际结果完成",
            )
            return subscription_id, "completed", "cancelled"
        queue.release_task(task_id=task_id, lease_token=lease_token, cancelled=True)
        return None, "cancelled", "cancelled"
    queue.finish_task(task_id=task_id, lease_token=lease_token, state="completed")
    return subscription_id, "completed", None


@dataclass(slots=True)
class MatchExecutionSummary:
    """保存一次 Match 批次的实际订阅执行结果。"""

    total: int = 0
    candidate_count: int = 0
    site_count: int = 0
    run_id: str = field(default_factory=lambda: uuid4().hex)
    started_at: float = field(default_factory=time.perf_counter)
    completed: int = 0
    skipped: int = 0
    failed: int = 0
    admission_conflicts: int = 0
    cancelled: int = 0
    ttl_timeouts: int = 0
    release_failures: int = 0

    @classmethod
    def from_candidates(
        cls,
        candidates: Mapping[str, Collection[object]],
    ) -> MatchExecutionSummary:
        """按站点分组的完整候选快照初始化轮次计数。"""
        return cls(
            candidate_count=sum(len(contexts) for contexts in candidates.values()),
            site_count=len(candidates),
        )

    @property
    def finished(self) -> int:
        """返回已经收口的订阅数量。"""
        return self.completed + self.skipped + self.failed

    def record(self, outcome: Optional[str], reason: Optional[str] = None) -> None:
        """将单订阅结果归入稳定的完成、跳过或失败计数。"""
        if outcome == "completed":
            self.completed += 1
        elif outcome == "skipped":
            self.skipped += 1
        else:
            self.failed += 1
        if reason == "admission_conflict":
            self.admission_conflicts += 1
        elif reason == "cancelled":
            self.cancelled += 1
        elif reason == "ttl_timeout":
            self.ttl_timeouts += 1

    @property
    def state(self) -> str:
        """按已处理数量和任务终态生成轮次状态。"""
        if self.finished < self.total:
            return "stopped"
        if self.failed:
            return "failed"
        if self.skipped:
            return "skipped"
        return "completed"

    def start_log(self) -> str:
        """构造 Match 轮次开始日志。"""
        return (
            "订阅治理轮次开始: operation=match "
            f"run_id={self.run_id} subscriptions={self.total} "
            f"sites={self.site_count} candidates={self.candidate_count}"
        )

    def finish_log(self) -> str:
        """构造 Match 轮次结束日志，completed 表示任务处理完成而非订阅成功。"""
        elapsed_ms = (time.perf_counter() - self.started_at) * 1000
        return (
            "订阅治理轮次结束: operation=match "
            f"run_id={self.run_id} state={self.state} subscriptions={self.total} "
            f"processed={self.finished} task_completed={self.completed} "
            f"task_skipped={self.skipped} task_failed={self.failed} "
            f"admission_conflicts={self.admission_conflicts} cancelled={self.cancelled} "
            f"ttl_timeouts={self.ttl_timeouts} release_failures={self.release_failures} "
            f"sites={self.site_count} candidates={self.candidate_count} "
            f"duration_ms={elapsed_ms:.1f}"
        )

    def as_data(self, current: Optional[int] = None) -> dict[str, int]:
        """构造供进度消费者读取的实际计数。"""
        data = {
            "total": self.total,
            "finished": self.finished,
            "completed": self.completed,
            "skipped": self.skipped,
            "failed": self.failed,
        }
        if current is not None:
            data["current"] = current
        return data


@dataclass(slots=True)
class SearchExecutionSummary:
    """聚合一次 Search 消费轮次的任务、站点与候选观测。"""

    source: str
    requested: int
    run_id: str = field(default_factory=lambda: uuid4().hex)
    batch_id: Optional[str] = None
    coalesced: int = 0
    started_at: float = field(default_factory=time.perf_counter)
    completed: int = 0
    skipped: int = 0
    failed: int = 0
    cancelled: int = 0
    requeued: int = 0
    admission_conflicts: int = 0
    ttl_timeouts: int = 0
    release_failures: int = 0
    consumer_conflicts: int = 0
    stopped: bool = False
    round_failed: bool = False
    site_metrics: SubscriptionSiteBudgetMetrics = field(
        default_factory=SubscriptionSiteBudgetMetrics
    )

    @property
    def processed(self) -> int:
        """返回本轮实际取得并收口或退回队列的任务数。"""
        return self.completed + self.skipped + self.failed + self.cancelled + self.requeued

    def record(self, outcome: SearchTaskOutcome, reason: Optional[str] = None) -> None:
        """记录一个任务结果，并单独统计冲突、取消和 TTL。"""
        if outcome == "completed":
            self.completed += 1
        elif outcome == "skipped":
            self.skipped += 1
        elif outcome == "failed":
            self.failed += 1
        elif outcome == "cancelled":
            self.cancelled += 1
        else:
            self.requeued += 1
        if reason == "admission_conflict":
            self.admission_conflicts += 1
        elif reason == "ttl_timeout":
            self.ttl_timeouts += 1

    def start_log(self) -> str:
        """构造 Search 轮次开始日志。"""
        return (
            "订阅治理轮次开始: operation=search "
            f"run_id={self.run_id} batch_id={self.batch_id or '-'} source={self.source} "
            f"subscriptions={self.requested} coalesced={self.coalesced}"
        )

    def finish_log(self, batch: Optional[SearchBatchSnapshot] = None) -> str:
        """构造 Search 轮次结束日志，任务完成与订阅完成保持分离。"""
        sites = self.site_metrics.snapshot()
        if self.round_failed:
            state = "failed"
        elif batch is not None:
            state = batch.state
        elif self.consumer_conflicts:
            state = "skipped"
        elif self.requeued or self.stopped:
            state = "stopped"
        elif self.failed:
            state = "failed"
        elif self.cancelled:
            state = "cancelled"
        elif self.skipped:
            state = "skipped"
        else:
            state = "completed"
        elapsed_ms = (time.perf_counter() - self.started_at) * 1000
        return (
            "订阅治理轮次结束: operation=search "
            f"run_id={self.run_id} batch_id={self.batch_id or '-'} source={self.source} "
            f"state={state} subscriptions={self.requested} processed={self.processed} "
            f"task_completed={self.completed} task_skipped={self.skipped} "
            f"task_failed={self.failed} task_cancelled={self.cancelled} task_requeued={self.requeued} "
            f"admission_conflicts={self.admission_conflicts} cancelled={self.cancelled} "
            f"ttl_timeouts={self.ttl_timeouts} consumer_conflicts={self.consumer_conflicts} "
            f"round_failed={int(self.round_failed)} "
            f"sites={sites.site_count} site_requests={sites.request_count} "
            f"site_failures={sites.failure_count} site_cooldown_skips={sites.cooldown_skip_count} "
            f"candidates={sites.candidate_count} cooldown_seconds={sites.cooldown_seconds:.1f} "
            f"release_failures={self.release_failures + sites.release_failure_count} "
            f"duration_ms={elapsed_ms:.1f}"
        )
