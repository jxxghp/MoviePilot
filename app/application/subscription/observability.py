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

_SEARCH_SOURCE_NAMES = {
    "manual": "手动订阅搜索",
    "targeted": "指定订阅搜索",
    "new": "新订阅自动搜索",
    "fallback": "订阅定时检查",
    "resume": "等待中的订阅搜索",
    "inline": "订阅搜索",
}

_SEARCH_STATE_NAMES = {
    "completed": "已完成",
    "failed": "部分订阅没有完成",
    "cancelled": "已停止",
    "skipped": "部分订阅这次未搜索",
    "queued": "部分订阅稍后继续",
    "running": "仍在处理中",
    "cancelling": "正在停止",
    "stopped": "部分订阅稍后继续",
}

_MATCH_STATE_NAMES = {
    "completed": "检查完成",
    "failed": "部分订阅没有完成",
    "cancelled": "已停止",
    "skipped": "部分订阅这次未检查",
    "stopped": "已停止，部分订阅这次未检查",
}


def _search_source_name(source: str) -> str:
    """返回搜索来源对应的可读名称。"""
    return _SEARCH_SOURCE_NAMES.get(source, "订阅搜索")


def _search_state_name(state: str) -> str:
    """返回搜索结束状态对应的可读说明。"""
    return _SEARCH_STATE_NAMES.get(state, "已结束")


def _match_state_name(state: str) -> str:
    """返回订阅资源检查状态对应的可读说明。"""
    return _MATCH_STATE_NAMES.get(state, "已结束")


def batch_progress_text(batch: Optional[SearchBatchSnapshot]) -> str:
    """把批次聚合终态转为兼容进度文案。"""
    if batch is None:
        return "搜索已安排"
    if batch.state == "failed":
        return "搜索结束，部分订阅没有完成"
    if batch.state == "cancelled":
        return "搜索已停止"
    if batch.state == "skipped":
        return "搜索结束，部分订阅这次未搜索"
    if batch.state in {"queued", "running", "cancelling"}:
        return "搜索已安排，系统正在依次处理"
    if batch.skipped_count:
        return "搜索结束，部分订阅这次未搜索"
    return "搜索完成"


def inline_search_result(total: int, finished: int) -> tuple[str, dict[str, int]]:
    """返回兼容搜索的真实终态文案与计数。"""
    text = (
        "搜索完成"
        if finished == total
        else "搜索结束，部分订阅这次没有完成"
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
                error="下载已经提交，虽然搜索用时较长，结果仍然有效",
            )
            return subscription_id, "completed", "ttl_timeout"
        queue.finish_task(
            task_id=task_id,
            lease_token=lease_token,
            state="failed",
            error="这次搜索用时过长，已停止，可稍后重试",
        )
        return None, "failed", "ttl_timeout"
    if system_stopped:
        if download_started:
            queue.finish_task(
                task_id=task_id,
                lease_token=lease_token,
                state="completed",
                error="下载已经提交，系统停止后仍保留这次结果",
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
                error="下载已经提交，无法撤回，已保留这次结果",
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
        """构造不暴露内部标记的资源检查开始日志。"""
        return (
            f"开始检查订阅资源，共 {self.total} 个订阅、{self.candidate_count} 个资源，"
            f"来自 {self.site_count} 个站点。"
        )

    def finish_log(self) -> str:
        """构造可直接阅读的资源检查结束日志。"""
        elapsed_seconds = time.perf_counter() - self.started_at
        message = (
            f"订阅资源检查结束：{_match_state_name(self.state)}。"
            f"本次检查 {self.finished}/{self.total} 个订阅，完成 {self.completed} 个，"
            f"这次未检查 {self.skipped} 个，失败 {self.failed} 个；"
            f"共检查 {self.candidate_count} 个资源，来自 {self.site_count} 个站点，"
            f"用时 {elapsed_seconds:.1f} 秒。"
        )
        if self.admission_conflicts:
            message += f"其中 {self.admission_conflicts} 个订阅正在处理中，本次没有重复检查。"
        if self.cancelled:
            message += f"另有 {self.cancelled} 个订阅已停止。"
        if self.ttl_timeouts:
            message += f"另有 {self.ttl_timeouts} 个订阅检查时间过长，已停止。"
        if self.release_failures:
            message += f"另有 {self.release_failures} 个订阅的搜索状态没有正常恢复，系统稍后会继续检查。"
        return message

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
        """构造不暴露内部术语的 Search 开始日志。"""
        message = f"开始{_search_source_name(self.source)}，共 {self.requested} 个订阅"
        if self.coalesced:
            message += f"，其中 {self.coalesced} 个已经在处理中"
        return f"{message}。"

    def finish_log(self, batch: Optional[SearchBatchSnapshot] = None) -> str:
        """构造可直接阅读的 Search 结束日志。"""
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
        elapsed_seconds = time.perf_counter() - self.started_at
        message = (
            f"{_search_source_name(self.source)}结束：{_search_state_name(state)}。"
            f"本次处理 {self.processed}/{self.requested} 个，完成 {self.completed} 个，"
            f"这次未搜索 {self.skipped} 个，失败 {self.failed} 个，"
            f"稍后继续 {self.requeued} 个，已停止 {self.cancelled} 个；"
            f"访问 {sites.site_count} 个站点，发出 {sites.request_count} 次请求，"
            f"找到 {sites.candidate_count} 个资源，用时 {elapsed_seconds:.1f} 秒。"
        )
        unfinished_cleanup = self.release_failures + sites.release_failure_count
        if unfinished_cleanup:
            message += f"另有 {unfinished_cleanup} 个搜索状态没有正常恢复，系统稍后会继续处理。"
        return message
