"""
订阅搜索队列单任务执行。
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import partial
from typing import Callable, Optional

from app.application.subscription.contract import (
    SubscriptionRepository,
    SubscriptionSnapshot,
)
from app.application.subscription.execution import (
    SearchTaskSnapshot,
    SubscriptionExecutionAdmission,
    SubscriptionExecutionContext,
    SubscriptionExecutionLease,
    SubscriptionSearchRepository,
    SubscriptionSiteSearchFailed,
    handle_subscription_search_deferred,
)
from app.application.subscription.observability import (
    SearchExecutionSummary,
    finish_returned_search_task,
    subscription_log_scope,
)
from app.application.subscription.sitebudget import (
    SubscriptionSearchCancelled,
    SubscriptionSearchDeferred,
    SubscriptionSiteBudget,
)
from app.chain.search.facade import SearchChain
from app.runtime.log import logger
from app.runtime.stop import StopState

_FOREGROUND_RETRY_SECONDS = 5
_BACKGROUND_RETRY_SECONDS = 10


def retry_at_after(seconds: int) -> str:
    """
    返回指定秒数后的 UTC 时间，供短暂等待任务重新入队。
    """
    return (datetime.now(timezone.utc) + timedelta(seconds=max(1, seconds))).isoformat(
        timespec="seconds"
    )


def skip_search_task(
    queue: SubscriptionSearchRepository,
    task: SearchTaskSnapshot,
    reason: str,
) -> bool:
    """
    以 skipped 终态收口未执行任务，并保留可见原因。
    """
    if task.lease_token is None:
        return False
    return queue.finish_task(
        task_id=task.task_id,
        lease_token=task.lease_token,
        state="skipped",
        error=reason,
    )


def subscription_search_cancelled(
    repository: SubscriptionRepository,
    subscription_id: int,
    allow_paused_search: bool,
    cancelled: Callable[[], bool],
) -> bool:
    """
    自动搜索在安全边界重读暂停状态；显式补搜仍遵守取消和系统停止。
    """
    if cancelled():
        return True
    if allow_paused_search:
        return False
    current = repository.get(subscription_id)
    return current is None or current.state == "S"


@dataclass(slots=True)
class SubscriptionSearchTaskRunner:
    """
    执行一条已认领的订阅搜索任务，并负责完整收尾。

    :param queue: 保存任务状态、租约和站点预算的搜索队列仓库
    :param task: 当前已经认领、包含任务租约的搜索任务快照
    :param owner: 本批执行者标识，与任务标识组合为站点预算持有者
    :param searchchain: 本任务使用的资源搜索链，执行期间装配站点预算
    :param index: 当前任务在进度批次中的序号
    :param limit: 本批次计划处理的任务总数
    :param progress_callback: 接收批次进度的回调，None 表示没有外部进度接收方
    :param summary: 累计本批任务结局、站点访问与资源释放结果的统计
    :param subscription_repository: 读取当前订阅和暂停状态的仓库
    :param execution_admission: 为同一订阅授予并回收互斥执行权的准入服务
    :param execution_ttl: 订阅搜索执行权的有效秒数
    :param recent_retry_at: 根据订阅和任务来源计算延后时间，返回 UTC 时间文本或 None
    :param process_subscription: 执行单条订阅搜索，接收搜索链和执行上下文并返回最新快照或 None
    :param reset_subscription: 把搜索中的订阅恢复为正常状态并返回更新后快照
    :param report_progress: 调用进度接收方，接收订阅、当前序号、总数及完成标志
    :param stop_state: 系统停止信号，关闭时归还任务以便下次继续
    """

    queue: SubscriptionSearchRepository
    task: SearchTaskSnapshot
    owner: str
    searchchain: SearchChain
    index: int
    limit: int
    progress_callback: Optional[Callable[..., None]]
    summary: SearchExecutionSummary
    subscription_repository: SubscriptionRepository
    execution_admission: SubscriptionExecutionAdmission
    execution_ttl: int
    recent_retry_at: Callable[[SubscriptionSnapshot, str], Optional[str]]
    process_subscription: Callable[..., Optional[SubscriptionSnapshot]]
    reset_subscription: Callable[[SubscriptionSnapshot], SubscriptionSnapshot]
    report_progress: Callable[..., None]
    stop_state: StopState

    def execute(self) -> Optional[int]:
        """
        执行当前任务，返回实际进入搜索处理的订阅 ID。
        """
        with subscription_log_scope(self.task.subscription_id, self.task.source, str(self.task.task_id)):
            logger.scoped_info("开始处理订阅搜索任务")
            return self._execute()

    def _execute(self) -> Optional[int]:
        """
        在已绑定日志的任务作用域内执行搜索及等待判断。
        """
        lease_token = self.task.lease_token
        if lease_token is None:
            logger.error("这次订阅搜索暂时无法开始，系统稍后会重新处理")
            self.summary.record("failed", "missing_lease")
            return None

        task_id = str(self.task.task_id)
        cancelled = partial(self.queue.is_cancel_requested, task_id)
        if cancelled():
            self.queue.release_task(task_id=task_id, lease_token=lease_token, cancelled=True)
            self.summary.record("cancelled", "cancelled")
            logger.scoped_info("本轮搜索已取消")
            return None

        subscribe = self.subscription_repository.get(self.task.subscription_id)
        if subscribe is None:
            self._finish_missing_subscription(task_id, lease_token)
            return None
        if self._skip_paused_subscription(subscribe):
            return None

        self.report_progress(
            self.progress_callback,
            subscribe,
            self.index,
            self.limit,
        )
        recent_retry_at = self.recent_retry_at(subscribe, self.task.source)
        if recent_retry_at:
            self.queue.defer_task(
                task_id=task_id,
                lease_token=lease_token,
                available_at=recent_retry_at,
                phase="scheduled",
                message="订阅刚刚创建，保存好设置后会自动开始搜索",
            )
            self.summary.record("requeued", "recent_subscription")
            logger.scoped_info(f"订阅刚刚创建，搜索已安排在 {recent_retry_at} 继续")
            return None

        execution_lease = self.execution_admission.try_acquire(
            subscription_id=subscribe.id,
            operation="search",
            ttl_seconds=self.execution_ttl,
        )
        if execution_lease is None:
            self._handle_active_subscription(subscribe, task_id, lease_token)
            return None
        return self._execute_owned_task(
            subscribe=subscribe,
            task_id=task_id,
            lease_token=lease_token,
            execution_lease=execution_lease,
            cancelled=cancelled,
        )

    def _finish_missing_subscription(self, task_id: str, lease_token: str) -> None:
        """
        把已经删除的订阅任务标记为取消。
        """
        self.queue.finish_task(
            task_id=task_id,
            lease_token=lease_token,
            state="cancelled",
            error="订阅已删除",
        )
        self.summary.record("cancelled", "missing_subscription")

    def _skip_paused_subscription(self, subscribe: SubscriptionSnapshot) -> bool:
        """
        暂停后收口已入队的自动任务，保留显式补搜的执行权限。
        """
        if subscribe.state != "S" or self.task.source in {"manual", "targeted"}:
            return False
        skip_search_task(self.queue, self.task, "订阅已暂停，这次没有自动搜索")
        self.summary.record("skipped", "paused")
        logger.scoped_info("订阅已暂停，本轮不执行自动搜索")
        return True

    def _handle_active_subscription(
        self,
        subscribe: SubscriptionSnapshot,
        task_id: str,
        lease_token: str,
    ) -> None:
        """
        根据搜索来源延后或跳过正在处理的订阅。
        """
        if self.task.source in {"manual", "targeted", "new"}:
            retry_seconds = (
                _FOREGROUND_RETRY_SECONDS
                if self.task.source in {"manual", "targeted"}
                else _BACKGROUND_RETRY_SECONDS
            )
            self.queue.defer_task(
                task_id=task_id,
                lease_token=lease_token,
                available_at=retry_at_after(retry_seconds),
                phase="waiting_subscription",
                message="这个订阅正在处理，结束后会自动继续搜索",
            )
            self.summary.record("requeued", "admission_conflict")
            logger.debug(f"订阅《{subscribe.name}》正在处理，搜索会在结束后自动继续")
            return
        skip_search_task(
            self.queue,
            self.task,
            "这个订阅正在处理，本次自动检查无需重复执行",
        )
        self.summary.record("skipped", "admission_conflict")
        logger.scoped_info("订阅正在处理，本轮自动检查无需重复执行")

    def _execute_owned_task(
        self,
        *,
        subscribe: SubscriptionSnapshot,
        task_id: str,
        lease_token: str,
        execution_lease: SubscriptionExecutionLease,
        cancelled: Callable[[], bool],
    ) -> Optional[int]:
        """
        在持有订阅执行权时完成搜索、异常处理和资源释放。
        """
        phase_changed = partial(self._update_phase, task_id, lease_token)
        allow_paused_search = self.task.source in {"manual", "targeted"}
        execution_context = SubscriptionExecutionContext(
            lease=execution_lease,
            admission=self.execution_admission,
            task_id=task_id,
            source=self.task.source,
            task_lease=lease_token,
            cancel_requested=partial(
                subscription_search_cancelled,
                self.subscription_repository,
                self.task.subscription_id,
                allow_paused_search,
                lambda: cancelled() or self.stop_state.is_system_stopped,
            ),
            allow_paused_search=allow_paused_search,
            phase_changed=phase_changed,
            resuming_sites=self.task.pending_site_ids is not None,
        )
        current: Optional[SubscriptionSnapshot] = subscribe
        try:
            current = self.subscription_repository.get(self.task.subscription_id)
            if current is None:
                self._finish_missing_subscription(task_id, lease_token)
                return None
            if self._skip_paused_subscription(current):
                return None
            self.searchchain.configure_subscription_site_budget(
                SubscriptionSiteBudget(
                    repository=self.queue,
                    owner=f"{self.owner}:{task_id}",
                    cancelled=execution_context.should_stop,
                    stop_state=self.stop_state,
                    phase_changed=phase_changed,
                    metrics=self.summary.site_metrics,
                    pending_site_ids=self.task.pending_site_ids,
                )
            )
            current = self.process_subscription(
                current,
                self.searchchain,
                execution_context=execution_context,
            )
            system_stopped = self.stop_state.is_system_stopped
            cancel_requested = False if system_stopped else execution_context.is_cancel_requested()
            subscription_id, outcome, reason = finish_returned_search_task(
                queue=self.queue,
                task_id=task_id,
                lease_token=lease_token,
                subscription_id=self.task.subscription_id,
                execution_context=execution_context,
                system_stopped=system_stopped,
                cancel_requested=cancel_requested,
            )
            self.summary.record(outcome, reason)
            logger.scoped_info(f"本轮搜索任务结束：{outcome}，原因：{reason or '正常结束'}；订阅是否完成以缺失内容检查为准")
            return subscription_id
        except SubscriptionSearchCancelled:
            self._handle_cancelled_search(task_id, lease_token, execution_context)
        except SubscriptionSearchDeferred as deferred:
            logger.scoped_info(f"站点暂时忙，本轮搜索延后继续：{deferred}")
            handle_subscription_search_deferred(
                self.queue,
                task_id,
                lease_token,
                deferred,
                self.summary.record,
            )
        except Exception as err:
            logger.error(
                f"订阅《{subscribe.name}》搜索失败：{str(err)}",
                exc_info=not isinstance(err, SubscriptionSiteSearchFailed),
            )
            self.queue.finish_task(
                task_id=task_id,
                lease_token=lease_token,
                state="failed",
                error=str(err),
            )
            self.summary.record("failed", "error")
        finally:
            self._cleanup(subscribe, current, execution_lease)
        return None

    def _update_phase(
        self,
        task_id: str,
        lease_token: str,
        phase: str,
        current_site_id: Optional[int] = None,
    ) -> None:
        """
        保存当前任务的用户可见阶段和正在访问的站点。
        """
        logger.debug(f"执行阶段：{phase}，当前站点：{current_site_id or '-'}")
        self.queue.update_task_phase(
            task_id=task_id,
            lease_token=lease_token,
            phase=phase,
            current_site_id=current_site_id,
        )

    def _handle_cancelled_search(
        self,
        task_id: str,
        lease_token: str,
        execution_context: SubscriptionExecutionContext,
    ) -> None:
        """
        区分搜索超时、用户停止和系统关闭。
        """
        logger.scoped_info("本轮搜索已停止，将根据取消、超时或系统关闭状态收尾")
        if execution_context.is_expired() and not execution_context.is_cancel_requested():
            self.queue.finish_task(
                task_id=task_id,
                lease_token=lease_token,
                state="failed",
                error="这次搜索用时过长，已停止，可稍后重试",
            )
            self.summary.record("failed", "ttl_timeout")
            return
        system_stopped = self.stop_state.is_system_stopped
        self.queue.release_task(
            task_id=task_id,
            lease_token=lease_token,
            cancelled=not system_stopped,
        )
        self.summary.record(
            "requeued" if system_stopped else "cancelled",
            "system_stop" if system_stopped else "cancelled",
        )

    def _cleanup(
        self,
        subscribe: SubscriptionSnapshot,
        current: Optional[SubscriptionSnapshot],
        execution_lease: SubscriptionExecutionLease,
    ) -> None:
        """
        清理站点访问、订阅状态和本轮执行权。
        """
        try:
            self.searchchain.configure_subscription_site_budget(None)
        except Exception as err:
            logger.error(
                f"订阅《{subscribe.name}》结束站点访问时遇到问题，"
                f"系统稍后会继续处理：{str(err)}",
                exc_info=True,
            )
        try:
            if current and current.state == "N":
                self.reset_subscription(current)
        except Exception as err:
            logger.error(
                f"订阅《{subscribe.name}》搜索结束后未能恢复正常状态：{str(err)}",
                exc_info=True,
            )
        finally:
            released = self.execution_admission.release(execution_lease)
            if not released:
                self.summary.release_failures += 1
                logger.error(f"订阅《{subscribe.name}》的搜索状态没有正常恢复，系统稍后会继续处理")
            self.report_progress(
                self.progress_callback,
                subscribe,
                self.index,
                self.limit,
                finished=True,
            )
