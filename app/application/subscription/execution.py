"""
订阅执行准入、搜索上下文、批次任务与持久队列端口。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Mapping, Optional, Protocol
from uuid import uuid4

from app.application.search.session import SearchSessionRepository
from app.application.subscription.sitebudget import (
    SiteBudgetClaim,
    SubscriptionSearchDeferred,
    SubscriptionSiteBudgetDeferral,
)
from app.schemas.common import JsonData


@dataclass(frozen=True, slots=True)
class SubscriptionExecutionLease:
    """
    一次订阅执行的进程内所有权及协作截止时间。

    :param subscription_id: 订阅记录的数据库主键
    :param operation: 持有者执行的操作类型，区分搜索和资源匹配
    :param owner_token: 标识本次进程内所有权的随机令牌
    :param expires_at: 协作执行截止的单调时钟时间，单位为秒
    """

    subscription_id: int
    operation: str
    owner_token: str
    expires_at: float


class SubscriptionExecutionAdmission:
    """
    按订阅 ID 控制 Search 与 Match 的进程内互斥。
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        """
        保存单调时钟和当前活跃持有者。
        """
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
        """
        无等待取得订阅所有权；已有持有者 时直接返回空。
        """
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
        """
        仅允许当前持有者 释放订阅所有权。
        """
        with self._lock:
            current = self._owners.get(lease.subscription_id)
            if current is None or current.owner_token != lease.owner_token:
                return False
            self._owners.pop(lease.subscription_id)
            return True

    def is_expired(self, lease: SubscriptionExecutionLease) -> bool:
        """
        判断持有者 是否已超过协作执行截止时间。
        """
        return self._clock() >= lease.expires_at


class SubscriptionSiteSearchFailed(RuntimeError):
    """
    表示本轮没有搜索源成功完成，无需输出内部异常堆栈。
    """


@dataclass(slots=True)
class SubscriptionExecutionContext:
    """
    一次订阅执行的显式取消、阶段和副作用边界。

    :param lease: 本次执行持有的进程内租约
    :param admission: 负责检验与释放该租约的准入管理器
    :param task_id: 持久搜索任务的稳定标识；无队列任务时为 None
    :param cancel_requested: 检查外部取消请求的回调；None 表示没有外部取消源
    :param phase_changed: 阶段变化时接收阶段名及当前站点标识的回调；None 表示不报告
    :param download_started: 是否已开始产生下载提交副作用
    :param resuming_sites: 是否从已保存的站点进度恢复
    :param incremental_search: 是否按增量分页方式推进候选收集
    :param scan_finished: 本轮候选扫描是否已完成
    :param task_lease: 持久搜索任务的租约令牌；非队列执行时为 None
    :param allow_paused_search: 是否允许手工或指定订阅补搜在暂停状态下提交下载
    :param source: 触发本轮执行的入口，例如手工搜索或自动调度
    :param refresh_sites: 本轮需要更新进度的站点摘要
    :param refresh_candidates: 刷新站点进度时累计的候选资源数
    """

    lease: SubscriptionExecutionLease
    admission: SubscriptionExecutionAdmission
    task_id: Optional[str] = None
    cancel_requested: Optional[Callable[[], bool]] = None
    phase_changed: Optional[Callable[[str, Optional[int]], None]] = None
    download_started: bool = False
    resuming_sites: bool = False
    incremental_search: bool = False
    scan_finished: bool = False
    task_lease: Optional[str] = None
    allow_paused_search: bool = False
    source: str = "automatic"
    refresh_sites: tuple[dict[str, object], ...] = ()
    refresh_candidates: int = 0

    def is_cancel_requested(self) -> bool:
        """
        判断调用入口是否请求在下一个安全边界退出。
        """
        return bool(self.cancel_requested and self.cancel_requested())

    def is_expired(self) -> bool:
        """
        判断本次执行是否已经超过协作截止时间。
        """
        return self.admission.is_expired(self.lease)

    def should_stop(self) -> bool:
        """
        在安全边界合并用户取消和执行 TTL。
        """
        return self.is_expired() or self.is_cancel_requested()

    def report_phase(self, phase: str, current_site_id: Optional[int] = None) -> None:
        """
        向当前搜索任务报告业务阶段。
        """
        if self.phase_changed:
            self.phase_changed(phase, current_site_id)

    def mark_download_started(self) -> None:
        """
        标记执行已越过下载器副作用边界。
        """
        self.download_started = True
        self.report_phase("submitting")


def raise_subscription_site_budget_failures(failures: tuple[str, ...]) -> None:
    """
    在所有实际搜索源均未成功时暴露站点聚合失败。
    """
    if failures:
        raise SubscriptionSiteSearchFailed("；".join(failures))


def raise_subscription_site_budget_deferral(
    deferrals: tuple[SubscriptionSiteBudgetDeferral, ...],
    execution_context: Optional[SubscriptionExecutionContext],
) -> None:
    """
    在没有下载副作用时，将临时站点冲突转换为持久队列延后。
    """
    if not deferrals or (execution_context and execution_context.download_started and not execution_context.incremental_search):
        return
    retry_at = min(deferrals, key=lambda item: item.retry_at).retry_at
    site_ids = tuple(dict.fromkeys(item.site_id for item in deferrals))
    wait_reason = "cooldown" if all(item.wait_reason == "cooldown" for item in deferrals) else "busy"
    raise SubscriptionSearchDeferred(retry_at=retry_at, site_ids=site_ids, wait_reason=wait_reason)


def handle_subscription_search_deferred(
    queue: SubscriptionSearchRepository,
    task_id: str,
    lease_token: str,
    deferred: SubscriptionSearchDeferred,
    record: Callable[..., None],
) -> None:
    """
    把站点暂时不可用的任务重新入队，而不是记录为搜索失败。
    """
    requeued = queue.defer_task(
        task_id=task_id,
        lease_token=lease_token,
        available_at=deferred.retry_at,
        phase="waiting_site_budget",
        message=str(deferred),
        pending_site_ids=deferred.site_ids,
    )
    if requeued:
        record("requeued", "site_budget_deferred")


@dataclass(frozen=True, slots=True)
class SearchBatchSnapshot:
    """
    订阅搜索批次的持久业务状态快照。

    :param batch_id: 持久搜索批次的稳定标识
    :param source: 触发本轮执行的入口，例如手工搜索或自动调度
    :param state: 当前任务或批次的业务状态
    :param priority: 队列调度优先级，数值较高者优先认领
    :param total_count: 批次包含的任务总数
    :param finished_count: 成功完成的任务数量
    :param failed_count: 失败终止的任务数量
    :param cancelled_count: 已取消的任务数量
    :param cancel_requested: 是否已请求在安全边界取消执行
    :param created_at: 记录创建时间文本
    :param updated_at: 最近状态更新时间文本
    :param started_at: 开始执行的时间文本；尚未执行时为 None
    :param finished_at: 完成执行的时间文本；尚未完成时为 None
    :param last_error: 最近失败的错误摘要；无错误时为 None
    :param skipped_count: 本轮无需执行而跳过的任务数量
    """

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
    """
    一个可认领、恢复和取消的订阅搜索任务快照。

    :param task_id: 持久搜索任务的稳定标识
    :param batch_id: 持久搜索批次的稳定标识
    :param subscription_id: 订阅记录的数据库主键
    :param source: 触发本轮执行的入口，例如手工搜索或自动调度
    :param priority: 队列调度优先级，数值较高者优先认领
    :param position: 任务在所属批次中的零基排列位置
    :param state: 当前任务或批次的业务状态
    :param phase: 当前执行阶段，用于展示进度与恢复位置
    :param attempt_count: 任务累计被认领执行的次数
    :param cancel_requested: 是否已请求在安全边界取消执行
    :param lease_token: 当前认领者的租约令牌；未取得租约时为 None
    :param created_at: 记录创建时间文本
    :param updated_at: 最近状态更新时间文本
    :param available_at: 任务最早可被认领的时间文本；None 表示无额外延迟
    :param started_at: 开始执行的时间文本；尚未执行时为 None
    :param finished_at: 完成执行的时间文本；尚未完成时为 None
    :param last_error: 最近失败的错误摘要；无错误时为 None
    :param current_site_id: 正在处理的站点标识；尚未进入站点时为 None
    :param download_counts: 任务活动累计的成功提交分类计数；旧活动为空
    :param downloads: 本任务活动中保留的成功下载安全摘要；最多 100 条
    :param pending_site_ids: 等待恢复的站点标识；None 表示尚未限定站点范围
    """

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
    pending_site_ids: Optional[tuple[int, ...]] = None
    download_counts: dict[str, int] = field(default_factory=dict)
    downloads: tuple[dict[str, JsonData], ...] = ()


@dataclass(frozen=True, slots=True)
class SearchEnqueueResult:
    """
    一次批次入队结果，区分新任务与 同订阅并发请求合并。

    :param batch: 本次入队对应的新建或既有批次快照
    :param created_count: 本次新建的任务数量
    :param coalesced_count: 与已有活动任务合并而未重复创建的数量
    :param active_batch_ids: 本次请求涉及的全部活动批次标识
    """

    batch: SearchBatchSnapshot
    created_count: int
    coalesced_count: int
    active_batch_ids: tuple[str, ...]


class SubscriptionSearchRepository(Protocol):
    """
    订阅搜索批次与任务的持久队列端口。

    实现方在独立短事务中提交队列状态，并以租约令牌隔离过期执行者。

    :ivar sessions: 保存和恢复搜索分页检查点的会话仓库
    """

    sessions: SearchSessionRepository

    def enqueue(
        self,
        *,
        subscription_ids: tuple[int, ...],
        source: str,
        priority: int,
        available_at_by_subscription: Optional[Mapping[int, str]] = None,
        refresh_pending: bool = False,
    ) -> SearchEnqueueResult:
        """
        建立或合并活动任务；新自动周期可恢复等待任务的完整搜索范围。

        :param subscription_ids: 目标订阅主键，保持调用方指定的顺序
        :param source: 搜索触发来源，用于调度和活动展示
        :param priority: 任务调度优先级，数值较高者优先认领
        :param available_at_by_subscription: 各订阅最早可执行时间；未指定的订阅立即具备调度资格
        :param refresh_pending: 是否为新周期重置尚未运行任务的站点搜索范围
        :return: 新建批次、创建及合并数量，以及涉及的活动批次标识
        """
        ...

    async def async_enqueue(
        self,
        *,
        subscription_ids: tuple[int, ...],
        source: str,
        priority: int,
        available_at_by_subscription: Optional[Mapping[int, str]] = None,
        refresh_pending: bool = False,
    ) -> SearchEnqueueResult:
        """
        异步建立或合并活动任务，可为新周期刷新尚未运行的站点游标。

        :param subscription_ids: 目标订阅主键，保持调用方指定的顺序
        :param source: 搜索触发来源，用于调度和活动展示
        :param priority: 任务调度优先级，数值较高者优先认领
        :param available_at_by_subscription: 各订阅最早可执行时间；未指定的订阅立即具备调度资格
        :param refresh_pending: 是否为新周期重置尚未运行任务的站点搜索范围
        :return: 新建批次、创建及合并数量，以及涉及的活动批次标识
        """
        ...

    def claim_next(self, *, owner: str, lease_seconds: int = 900) -> Optional[SearchTaskSnapshot]:
        """
        按优先级和稳定游标认领下一条可执行任务。

        :param owner: 本次认领者的稳定标识
        :param lease_seconds: 租约有效时长，单位为秒
        :return: 已认领且带租约令牌的任务；当前没有可执行任务时返回 None
        """
        ...

    def finish_task(
        self,
        *,
        task_id: str,
        lease_token: str,
        state: str,
        error: Optional[str] = None,
    ) -> bool:
        """
        以租约令牌收口任务，并推进所属批次聚合状态（含 skipped）。

        :param task_id: 持久搜索任务的稳定标识
        :param lease_token: 认领时取得的租约令牌，防止旧执行者覆盖新状态
        :param state: 拟写入的终止状态，例如 completed、failed、cancelled 或 skipped
        :param error: 可展示的失败摘要；无错误时为 None
        :return: 任务仍归该租约且成功收口时返回 True，否则返回 False
        """
        ...

    def update_task_phase(
        self,
        *,
        task_id: str,
        lease_token: str,
        phase: str,
        current_site_id: Optional[int] = None,
    ) -> bool:
        """
        以当前租约令牌更新用户可见阶段和正在处理的站点。

        :param task_id: 持久搜索任务的稳定标识
        :param lease_token: 认领时取得的租约令牌，防止旧执行者覆盖新状态
        :param phase: 任务当前阶段名称，用于进度展示和恢复
        :param current_site_id: 当前处理的站点主键；None 表示未进入站点
        :return: 任务仍归该租约且阶段写入成功时返回 True，否则返回 False
        """
        ...

    def release_task(
        self,
        *,
        task_id: str,
        lease_token: str,
        cancelled: bool = False,
    ) -> bool:
        """
        释放尚未完成的任务租约，供停止或取消后恢复。

        :param task_id: 持久搜索任务的稳定标识
        :param lease_token: 认领时取得的租约令牌，防止旧执行者覆盖新状态
        :param cancelled: 是否将任务按取消状态结束；False 表示重新排队
        :return: 当前租约成功释放或取消收口时返回 True，否则返回 False
        """
        ...

    def defer_task(
        self,
        *,
        task_id: str,
        lease_token: str,
        available_at: str,
        phase: str = "waiting_site_budget",
        message: Optional[str] = None,
        pending_site_ids: Optional[tuple[int, ...]] = None,
    ) -> bool:
        """
        延后任务并保存等待原因与待搜站点；省略站点时保留已有游标。

        :param task_id: 持久搜索任务的稳定标识
        :param lease_token: 认领时取得的租约令牌，防止旧执行者覆盖新状态
        :param available_at: 下一次允许认领任务的 UTC 时间文本
        :param phase: 任务当前阶段名称，用于进度展示和恢复
        :param message: 延后原因的可读说明；None 表示无附加说明
        :param pending_site_ids: 尚未完成的站点标识；None 保留已有站点进度
        :return: 当前租约成功转为等待或因取消请求结束时返回 True；租约失效时返回 False
        """
        ...

    def is_cancel_requested(self, task_id: str) -> bool:
        """
        判断任务或所属批次是否已请求取消。

        :param task_id: 持久搜索任务的稳定标识
        :return: 任务或所属批次已经请求取消时返回 True
        """
        ...

    def request_cancel(self, batch_id: str) -> bool:
        """
        请求取消批次，并立即终止尚未发出的排队任务。

        :param batch_id: 持久搜索批次的稳定标识
        :return: 找到可取消的批次并接受请求时返回 True；批次不存在或已完成、失败、取消时返回 False
        """
        ...

    def get_batch(self, batch_id: str) -> Optional[SearchBatchSnapshot]:
        """
        按稳定批次 ID 返回当前聚合状态。

        :param batch_id: 持久搜索批次的稳定标识
        :return: 对应批次的最新快照；不存在时返回 None
        """
        ...

    def claim_site(
        self,
        *,
        site_id: int,
        owner: str,
        lease_seconds: int,
        minimum_interval: float = 0,
    ) -> SiteBudgetClaim:
        """
        认领一个站点的唯一在途搜索预算。

        :param site_id: 目标站点配置的主键
        :param owner: 本次认领者的稳定标识
        :param lease_seconds: 租约有效时长，单位为秒
        :param minimum_interval: 两次站点请求的最小间隔，单位为秒，默认 0 不加额外间隔
        :return: 包含认领结果、当前租约或等待原因与重试时间的快照
        """
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
        """
        释放站点预算并写入错误冷却和恢复状态。

        :param site_id: 目标站点配置的主键
        :param lease_token: 认领时取得的租约令牌，防止旧执行者覆盖新状态
        :param outcome: 本次站点请求的结果分类，用于决定失败累计和冷却
        :param next_allowed_at: 站点下一次允许请求的 UTC 时间文本
        :param error: 可展示的失败摘要；无错误时为 None
        :return: 租约仍匹配且完成释放时返回 True，否则返回 False
        """
        ...
