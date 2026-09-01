"""订阅搜索执行批次、任务与持久队列端口。"""

from dataclasses import dataclass
from typing import Optional, Protocol

from app.application.subscription.sitebudget import SiteBudgetClaim


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


class SubscriptionSearchRepository(Protocol):
    """订阅搜索批次与任务的持久队列端口。"""

    def enqueue(
        self,
        *,
        subscription_ids: tuple[int, ...],
        source: str,
        priority: int,
        available_at: Optional[str] = None,
    ) -> SearchEnqueueResult:
        """按订阅 ID 建立批次，在启动抖动后合并活动任务。"""
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
        """以租约令牌收口任务，并推进所属批次聚合状态。"""
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
        """释放站点预算并写入间隔、冷却和恢复状态。"""
        ...
