"""订阅搜索持久队列的 SQLAlchemy 适配器。"""

from collections.abc import Callable
from typing import Optional, TypeVar

from sqlalchemy.orm import Session

from app.application.subscription.execution import (
    SearchBatchSnapshot,
    SearchEnqueueResult,
    SearchTaskSnapshot,
)
from app.db.models.subscriptionsearch import (
    SubscriptionSearchBatch,
    SubscriptionSearchTask,
)
from app.db.oper.subscriptionsearch import SubscriptionSearchOper
from app.db.uow import SqlAlchemyUnitOfWork

T = TypeVar("T")


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
        attempt_count=record.attempt_count,
        cancel_requested=bool(record.cancel_requested),
        lease_token=record.lease_token,
        created_at=record.created_at,
        updated_at=record.updated_at,
        started_at=record.started_at,
        finished_at=record.finished_at,
        last_error=record.last_error,
    )


class TransactionalSubscriptionSearchRepository:
    """使用短事务实现订阅搜索队列端口。"""

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        """保存由组合根注入的同步 Session 工厂。"""
        self._session_factory = session_factory

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

    def enqueue(
        self,
        *,
        subscription_ids: tuple[int, ...],
        source: str,
        priority: int,
    ) -> SearchEnqueueResult:
        """创建批次并返回 single-flight 合并计数。"""
        def operation(repository: SubscriptionSearchOper) -> SearchEnqueueResult:
            """在同一事务内创建批次和任务。"""
            record, created, coalesced = repository.enqueue(
                subscription_ids=subscription_ids,
                source=source,
                priority=priority,
            )
            return SearchEnqueueResult(
                batch=_batch(record),
                created_count=created,
                coalesced_count=coalesced,
            )

        return self._write(operation)

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
