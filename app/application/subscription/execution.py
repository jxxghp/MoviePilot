"""订阅执行准入、搜索上下文、批次任务与持久队列端口。"""

import threading
import time
from dataclasses import dataclass
from typing import Callable, Mapping, Optional, Protocol
from uuid import uuid4

from app.application.subscription.sitebudget import (
    SiteBudgetClaim,
    SubscriptionSearchDeferred,
    SubscriptionSiteBudgetDeferral,
)


@dataclass(frozen=True, slots=True)
class SubscriptionExecutionLease:
    """一次订阅执行的进程内所有权及协作截止时间。"""

    subscription_id: int
    operation: str
    owner_token: str
    expires_at: float


class SubscriptionExecutionAdmission:
    """按订阅 ID 控制 Search 与 Match 的进程内互斥。"""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        """保存单调时钟和当前活跃 owner。"""
        self._clock = clock
        self._lock = threading.Lock()
        self._owners: dict[int, SubscriptionExecutionLease] = {}

    def try_acquire(
        self,
        *,
        subscription_id: int,
        operation: str,
        ttl_seconds: float,
    ) -> Optional[SubscriptionExecutionLease]:
        """无等待取得订阅所有权；已有 owner 时直接返回空。"""
        with self._lock:
            if subscription_id in self._owners:
                return None
            lease = SubscriptionExecutionLease(
                subscription_id=subscription_id,
                operation=operation,
                owner_token=uuid4().hex,
                expires_at=self._clock() + max(1.0, ttl_seconds),
            )
            self._owners[subscription_id] = lease
            return lease

    def release(self, lease: SubscriptionExecutionLease) -> bool:
        """仅允许当前 owner 释放订阅所有权。"""
        with self._lock:
            current = self._owners.get(lease.subscription_id)
            if current is None or current.owner_token != lease.owner_token:
                return False
            self._owners.pop(lease.subscription_id)
            return True

    def is_expired(self, lease: SubscriptionExecutionLease) -> bool:
        """判断 owner 是否已超过协作执行截止时间。"""
        return self._clock() >= lease.expires_at


@dataclass(slots=True)
class SubscriptionExecutionContext:
    """一次订阅执行的显式取消、阶段和副作用边界。"""

    lease: SubscriptionExecutionLease
    admission: SubscriptionExecutionAdmission
    task_id: Optional[str] = None
    cancel_requested: Optional[Callable[[], bool]] = None
    phase_changed: Optional[Callable[[str, Optional[int]], None]] = None
    download_started: bool = False

    def is_cancel_requested(self) -> bool:
        """判断调用入口是否请求在下一个安全边界退出。"""
        return bool(self.cancel_requested and self.cancel_requested())

    def is_expired(self) -> bool:
        """判断本次执行是否已经超过协作截止时间。"""
        return self.admission.is_expired(self.lease)

    def should_stop(self) -> bool:
        """在安全边界合并用户取消和执行 TTL。"""
        return self.is_expired() or self.is_cancel_requested()

    def report_phase(self, phase: str, current_site_id: Optional[int] = None) -> None:
        """向当前搜索任务报告业务阶段。"""
        if self.phase_changed:
            self.phase_changed(phase, current_site_id)

    def mark_download_started(self) -> None:
        """标记执行已越过下载器副作用边界。"""
        self.download_started = True
        self.report_phase("submitting")


def raise_subscription_site_budget_failures(failures: tuple[str, ...]) -> None:
    """在成功站点结果完成处理后暴露其余站点的聚合失败。"""
    if failures:
        raise RuntimeError("；".join(failures))


def raise_subscription_site_budget_deferral(
    deferrals: tuple[SubscriptionSiteBudgetDeferral, ...],
    execution_context: Optional[SubscriptionExecutionContext],
) -> None:
    """在没有下载副作用时，将临时站点冲突转换为持久队列延后。"""
    if not deferrals or (execution_context and execution_context.download_started):
        return
    retry_at = min(deferrals, key=lambda item: item.retry_at).retry_at
    site_ids = tuple(dict.fromkeys(item.site_id for item in deferrals))
    raise SubscriptionSearchDeferred(retry_at=retry_at, site_ids=site_ids)


def handle_subscription_search_deferred(
    queue: SubscriptionSearchRepository,
    task_id: str,
    lease_token: str,
    deferred: SubscriptionSearchDeferred,
    record: Callable[..., None],
) -> None:
    """把站点暂时不可用的任务重新入队，而不是记录为搜索失败。"""
    requeued = queue.defer_task(
        task_id=task_id,
        lease_token=lease_token,
        available_at=deferred.retry_at,
        phase="waiting_site_budget",
        message="站点暂时忙，系统会自动继续搜索",
    )
    if requeued:
        record("requeued", "site_budget_deferred")


@dataclass(frozen=True, slots=True)
class SearchBatchSnapshot:
    """订阅搜索批次的持久业务状态快照。"""

    batch_id: str
    source: str
    state: str
    priority: int
    total_count: int
    finished_count: int
    failed_count: int
    cancelled_count: int
    cancel_requested: bool
    created_at: str
    updated_at: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    last_error: Optional[str] = None
    skipped_count: int = 0


@dataclass(frozen=True, slots=True)
class SearchTaskSnapshot:
    """一个可认领、恢复和取消的订阅搜索任务快照。"""

    task_id: str
    batch_id: str
    subscription_id: int
    source: str
    priority: int
    position: int
    state: str
    phase: str
    attempt_count: int
    cancel_requested: bool
    lease_token: Optional[str]
    created_at: str
    updated_at: str
    available_at: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    last_error: Optional[str] = None
    current_site_id: Optional[int] = None


@dataclass(frozen=True, slots=True)
class SearchEnqueueResult:
    """一次批次入队结果，区分新任务与 single-flight 合并。"""

    batch: SearchBatchSnapshot
    created_count: int
    coalesced_count: int
    active_batch_ids: tuple[str, ...]


class SubscriptionSearchRepository(Protocol):
    """订阅搜索批次与任务的持久队列端口。"""

    def enqueue(
        self,
        *,
        subscription_ids: tuple[int, ...],
        source: str,
        priority: int,
        available_at_by_subscription: Optional[Mapping[int, str]] = None,
    ) -> SearchEnqueueResult:
        """按订阅 ID 和各自到期时间建立或合并活动任务。"""
        ...

    async def async_enqueue(
        self,
        *,
        subscription_ids: tuple[int, ...],
        source: str,
        priority: int,
        available_at_by_subscription: Optional[Mapping[int, str]] = None,
    ) -> SearchEnqueueResult:
        """在异步会话中建立或合并活动任务。"""
        ...

    def claim_next(self, *, owner: str, lease_seconds: int = 900) -> Optional[SearchTaskSnapshot]:
        """按优先级和稳定游标认领下一条可执行任务。"""
        ...

    def finish_task(
        self,
        *,
        task_id: str,
        lease_token: str,
        state: str,
        error: Optional[str] = None,
    ) -> bool:
        """以租约令牌收口任务，并推进所属批次聚合状态（含 skipped）。"""
        ...

    def update_task_phase(
        self,
        *,
        task_id: str,
        lease_token: str,
        phase: str,
        current_site_id: Optional[int] = None,
    ) -> bool:
        """以当前租约令牌更新用户可见阶段和正在处理的站点。"""
        ...

    def release_task(
        self,
        *,
        task_id: str,
        lease_token: str,
        cancelled: bool = False,
    ) -> bool:
        """释放尚未完成的任务租约，供停止或取消后恢复。"""
        ...

    def defer_task(
        self,
        *,
        task_id: str,
        lease_token: str,
        available_at: str,
        phase: str = "waiting_site_budget",
        message: Optional[str] = None,
    ) -> bool:
        """把临时不可执行任务退回队列，并保留用户可理解的等待原因。"""
        ...

    def is_cancel_requested(self, task_id: str) -> bool:
        """判断任务或所属批次是否已请求取消。"""
        ...

    def request_cancel(self, batch_id: str) -> bool:
        """请求取消批次，并立即终止尚未发出的排队任务。"""
        ...

    def get_batch(self, batch_id: str) -> Optional[SearchBatchSnapshot]:
        """按稳定批次 ID 返回当前聚合状态。"""
        ...

    def claim_site(
        self,
        *,
        site_id: int,
        owner: str,
        lease_seconds: int,
    ) -> SiteBudgetClaim:
        """认领一个站点的唯一在途搜索预算。"""
        ...

    def finish_site(
        self,
        *,
        site_id: int,
        lease_token: str,
        outcome: str,
        next_allowed_at: str,
        error: Optional[str] = None,
    ) -> bool:
        """释放站点预算并写入错误冷却和恢复状态。"""
        ...
