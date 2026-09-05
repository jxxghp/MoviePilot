"""订阅搜索持久队列的 SQLAlchemy 适配器。"""

from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timedelta, timezone
from typing import Optional, TypeVar

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.application.subscription.execution import (
    SearchBatchSnapshot,
    SearchEnqueueResult,
    SearchTaskSnapshot,
)
from app.application.subscription.sitebudget import SiteBudgetClaim
from app.db.models.subscriptionsearch import (
    SubscriptionSearchBatch,
    SubscriptionSearchTask,
)
from app.db.oper.subscriptionsearch import SubscriptionSearchOper
from app.db.uow import SqlAlchemyAsyncUnitOfWork, SqlAlchemyUnitOfWork

T = TypeVar("T")
_BUSY_SITE_RETRY_SECONDS = 10


def _batch(record: SubscriptionSearchBatch) -> SearchBatchSnapshot:
    """在 Session 内投影不可变搜索批次快照。"""
    return SearchBatchSnapshot(
        batch_id=record.batch_id,
        source=record.source,
        state=record.state,
        priority=record.priority,
        total_count=record.total_count,
        finished_count=record.finished_count,
        failed_count=record.failed_count,
        cancelled_count=record.cancelled_count,
        skipped_count=record.skipped_count,
        cancel_requested=bool(record.cancel_requested),
        created_at=record.created_at,
        updated_at=record.updated_at,
        started_at=record.started_at,
        finished_at=record.finished_at,
        last_error=record.last_error,
    )


def _task(record: SubscriptionSearchTask) -> SearchTaskSnapshot:
    """在 Session 内投影不可变搜索任务快照。"""
    return SearchTaskSnapshot(
        task_id=record.task_id,
        batch_id=record.batch_id,
        subscription_id=record.subscription_id,
        source=record.source,
        priority=record.priority,
        position=record.position,
        state=record.state,
        phase=record.phase,
        attempt_count=record.attempt_count,
        cancel_requested=bool(record.cancel_requested),
        lease_token=record.lease_token,
        created_at=record.created_at,
        updated_at=record.updated_at,
        available_at=record.available_at,
        started_at=record.started_at,
        finished_at=record.finished_at,
        last_error=record.last_error,
        current_site_id=record.current_site_id,
    )


def _enqueue_result(
    repository: SubscriptionSearchOper,
    *,
    subscription_ids: tuple[int, ...],
    source: str,
    priority: int,
    available_at_by_subscription: Optional[Mapping[int, str]],
) -> SearchEnqueueResult:
    """在当前事务中创建搜索批次并投影返回结果。"""
    record, created, coalesced, active_batch_ids = repository.enqueue(
        subscription_ids=subscription_ids,
        source=source,
        priority=priority,
        available_at_by_subscription=available_at_by_subscription,
    )
    return SearchEnqueueResult(
        batch=_batch(record),
        created_count=created,
        coalesced_count=coalesced,
        active_batch_ids=active_batch_ids,
    )


class TransactionalSubscriptionSearchRepository:
    """使用同步或异步短事务实现订阅搜索队列端口。"""

    def __init__(
        self,
        session_factory: Callable[[], Session],
        async_session_factory: Optional[
            Callable[[], AbstractAsyncContextManager[AsyncSession]]
        ] = None,
    ) -> None:
        """保存由组合根注入的同步和异步 Session 工厂。"""
        self._session_factory = session_factory
        self._async_session_factory = async_session_factory

    def _read(self, operation: Callable[[SubscriptionSearchOper], T]) -> T:
        """在短 Session 中执行一次只读查询。"""
        with self._session_factory() as session:
            return operation(SubscriptionSearchOper(session))

    def _write(self, operation: Callable[[SubscriptionSearchOper], T]) -> T:
        """在短事务中执行一次队列状态变更。"""
        with self._session_factory() as session:
            unit_of_work = SqlAlchemyUnitOfWork(session)
            try:
                result = operation(SubscriptionSearchOper(session))
                unit_of_work.commit()
                return result
            except Exception:
                unit_of_work.rollback()
                raise

    async def _async_write(self, operation: Callable[[SubscriptionSearchOper], T]) -> T:
        """在短异步事务中执行一次队列状态变更。"""
        if self._async_session_factory is None:
            raise RuntimeError("订阅搜索异步写入尚未配置")
        async with self._async_session_factory() as session:
            unit_of_work = SqlAlchemyAsyncUnitOfWork(session)
            try:
                result = await session.run_sync(
                    lambda sync_session: operation(SubscriptionSearchOper(sync_session))
                )
                await unit_of_work.commit()
                return result
            except Exception:
                await unit_of_work.rollback()
                raise

    def enqueue(
        self,
        *,
        subscription_ids: tuple[int, ...],
        source: str,
        priority: int,
        available_at_by_subscription: Optional[Mapping[int, str]] = None,
    ) -> SearchEnqueueResult:
        """创建批次并返回 single-flight 合并计数。"""
        return self._write(
            lambda repository: _enqueue_result(
                repository,
                subscription_ids=subscription_ids,
                source=source,
                priority=priority,
                available_at_by_subscription=available_at_by_subscription,
            )
        )

    async def async_enqueue(
        self,
        *,
        subscription_ids: tuple[int, ...],
        source: str,
        priority: int,
        available_at_by_subscription: Optional[Mapping[int, str]] = None,
    ) -> SearchEnqueueResult:
        """在短异步事务中创建批次并返回合并计数。"""
        return await self._async_write(
            lambda repository: _enqueue_result(
                repository,
                subscription_ids=subscription_ids,
                source=source,
                priority=priority,
                available_at_by_subscription=available_at_by_subscription,
            )
        )

    def claim_next(self, *, owner: str, lease_seconds: int = 900) -> Optional[SearchTaskSnapshot]:
        """认领下一任务并返回脱离 Session 的快照。"""
        return self._write(
            lambda repository: (
                _task(record)
                if (record := repository.claim_next(owner=owner, lease_seconds=lease_seconds)) is not None
                else None
            )
        )

    def finish_task(
        self,
        *,
        task_id: str,
        lease_token: str,
        state: str,
        error: Optional[str] = None,
    ) -> bool:
        """以租约令牌收口任务。"""
        return self._write(
            lambda repository: repository.finish_task(
                task_id=task_id,
                lease_token=lease_token,
                state=state,
                error=error,
            )
        )

    def update_task_phase(
        self,
        *,
        task_id: str,
        lease_token: str,
        phase: str,
        current_site_id: Optional[int] = None,
    ) -> bool:
        """以当前租约更新任务阶段。"""
        return self._write(
            lambda repository: repository.update_task_phase(
                task_id=task_id,
                lease_token=lease_token,
                phase=phase,
                current_site_id=current_site_id,
            )
        )

    def release_task(
        self,
        *,
        task_id: str,
        lease_token: str,
        cancelled: bool = False,
    ) -> bool:
        """释放执行租约或收口取消。"""
        return self._write(
            lambda repository: repository.release_task(
                task_id=task_id,
                lease_token=lease_token,
                cancelled=cancelled,
            )
        )

    def defer_task(
        self,
        *,
        task_id: str,
        lease_token: str,
        available_at: str,
        phase: str = "waiting_site_budget",
        message: Optional[str] = None,
    ) -> bool:
        """按指定时间和可见原因重新排队任务。"""
        return self._write(
            lambda repository: repository.defer_task(
                task_id=task_id,
                lease_token=lease_token,
                available_at=available_at,
                phase=phase,
                message=message,
            )
        )

    def is_cancel_requested(self, task_id: str) -> bool:
        """查询任务或批次的取消请求。"""
        return self._read(lambda repository: repository.is_cancel_requested(task_id))

    def request_cancel(self, batch_id: str) -> bool:
        """请求取消批次。"""
        return self._write(lambda repository: repository.request_cancel(batch_id))

    def get_batch(self, batch_id: str) -> Optional[SearchBatchSnapshot]:
        """查询批次聚合快照。"""
        return self._read(
            lambda repository: (
                _batch(record) if (record := repository.get_batch(batch_id)) is not None else None
            )
        )

    def claim_site(
        self,
        *,
        site_id: int,
        owner: str,
        lease_seconds: int,
    ) -> SiteBudgetClaim:
        """认领单站点预算并投影等待或租约事实。"""
        def operation(repository: SubscriptionSearchOper) -> SiteBudgetClaim:
            """在短事务中认领并复制站点预算状态。"""
            record, acquired = repository.claim_site(
                site_id=site_id,
                owner=owner,
                lease_seconds=lease_seconds,
            )
            retry_at = record.next_allowed_at
            wait_reason = None
            now = datetime.now(timezone.utc)
            cooldown_active = bool(
                record.last_outcome not in {None, "success", "skipped"}
                and record.next_allowed_at > now.isoformat(timespec="seconds")
            )
            lease_busy = bool(
                record.lease_token
                and record.lease_expires_at
                and record.lease_expires_at > now.isoformat(timespec="seconds")
            )
            if not acquired and cooldown_active:
                wait_reason = "cooldown"
            elif not acquired and lease_busy:
                wait_reason = "busy"
                short_retry = (now + timedelta(seconds=_BUSY_SITE_RETRY_SECONDS)).isoformat(
                    timespec="seconds"
                )
                retry_at = min(record.lease_expires_at, short_retry)
            return SiteBudgetClaim(
                site_id=record.site_id,
                acquired=acquired,
                retry_at=retry_at,
                consecutive_failures=record.consecutive_failures,
                lease_token=record.lease_token if acquired else None,
                wait_reason=wait_reason,
            )

        return self._write(operation)

    def finish_site(
        self,
        *,
        site_id: int,
        lease_token: str,
        outcome: str,
        next_allowed_at: str,
        error: Optional[str] = None,
    ) -> bool:
        """释放站点租约并持久化错误冷却或立即恢复。"""
        return self._write(
            lambda repository: repository.finish_site(
                site_id=site_id,
                lease_token=lease_token,
                outcome=outcome,
                next_allowed_at=next_allowed_at,
                error=error,
            )
        )
