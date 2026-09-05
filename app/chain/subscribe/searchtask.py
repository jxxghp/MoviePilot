"""订阅搜索队列单任务执行。"""

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
    handle_subscription_search_deferred,
)
from app.application.subscription.observability import (
    SearchExecutionSummary,
    finish_returned_search_task,
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
    """返回指定秒数后的 UTC 时间，供短暂等待任务重新入队。"""
    return (datetime.now(timezone.utc) + timedelta(seconds=max(1, seconds))).isoformat(
        timespec="seconds"
    )


def skip_search_task(
    queue: SubscriptionSearchRepository,
    task: SearchTaskSnapshot,
    reason: str,
) -> bool:
    """以 skipped 终态收口未执行任务，并保留可见原因。"""
    if task.lease_token is None:
        return False
    return queue.finish_task(
        task_id=task.task_id,
        lease_token=task.lease_token,
        state="skipped",
        error=reason,
    )


@dataclass(slots=True)
class SubscriptionSearchTaskRunner:
    """执行一条已认领的订阅搜索任务，并负责完整收尾。"""

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
        """执行当前任务，返回实际进入搜索处理的订阅 ID。"""
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
            return None

        subscribe = self.subscription_repository.get(self.task.subscription_id)
        if subscribe is None:
            self._finish_missing_subscription(task_id, lease_token)
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
        """把已经删除的订阅任务标记为取消。"""
        self.queue.finish_task(
            task_id=task_id,
            lease_token=lease_token,
            state="cancelled",
            error="订阅已删除",
        )
        self.summary.record("cancelled", "missing_subscription")

    def _handle_active_subscription(
        self,
        subscribe: SubscriptionSnapshot,
        task_id: str,
        lease_token: str,
    ) -> None:
        """根据搜索来源延后或跳过正在处理的订阅。"""
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

    def _execute_owned_task(
        self,
        *,
        subscribe: SubscriptionSnapshot,
        task_id: str,
        lease_token: str,
        execution_lease: SubscriptionExecutionLease,
        cancelled: Callable[[], bool],
    ) -> Optional[int]:
        """在持有订阅执行权时完成搜索、异常处理和资源释放。"""
        phase_changed = partial(self._update_phase, task_id, lease_token)
        execution_context = SubscriptionExecutionContext(
            lease=execution_lease,
            admission=self.execution_admission,
            task_id=task_id,
            cancel_requested=lambda: cancelled() or self.stop_state.is_system_stopped,
            phase_changed=phase_changed,
        )
        current: Optional[SubscriptionSnapshot] = subscribe
        try:
            current = self.subscription_repository.get(self.task.subscription_id)
            if current is None:
                self._finish_missing_subscription(task_id, lease_token)
                return None
            if current.state == "S":
                skip_search_task(self.queue, self.task, "订阅已暂停，这次没有搜索")
                self.summary.record("skipped", "paused")
                return None
            self.searchchain.configure_subscription_site_budget(
                SubscriptionSiteBudget(
                    repository=self.queue,
                    owner=f"{self.owner}:{task_id}",
                    cancelled=execution_context.should_stop,
                    stop_state=self.stop_state,
                    phase_changed=phase_changed,
                    metrics=self.summary.site_metrics,
                )
            )
            current = self.process_subscription(
                current,
                self.searchchain,
                execution_context=execution_context,
            )
            system_stopped = self.stop_state.is_system_stopped
            cancel_requested = False if system_stopped else cancelled()
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
            return subscription_id
        except SubscriptionSearchCancelled:
            self._handle_cancelled_search(task_id, lease_token, execution_context)
        except SubscriptionSearchDeferred as deferred:
            handle_subscription_search_deferred(
                self.queue,
                task_id,
                lease_token,
                deferred,
                self.summary.record,
            )
        except Exception as err:
            logger.error(f"订阅《{subscribe.name}》搜索失败：{str(err)}", exc_info=True)
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
        """保存当前任务的用户可见阶段和正在访问的站点。"""
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
        """区分搜索超时、用户停止和系统关闭。"""
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
        """清理站点访问、订阅状态和本轮执行权。"""
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
