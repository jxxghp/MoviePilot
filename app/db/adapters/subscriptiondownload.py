"""订阅下载幂等账本的短事务适配器。"""

from collections.abc import Callable
from typing import Optional, TypeVar

from sqlalchemy.orm import Session

from app.application.download.admission import (
    SubscriptionDownloadClaim,
    SubscriptionDownloadRequest,
    SubscriptionDownloadSnapshot,
)
from app.db.models.subscriptiondownload import SubscriptionDownloadSubmission
from app.db.oper.subscriptiondownload import SubscriptionDownloadOper
from app.db.uow import SqlAlchemyUnitOfWork

T = TypeVar("T")


def _snapshot(record: SubscriptionDownloadSubmission) -> SubscriptionDownloadSnapshot:
    """在 Session 内投影不可变提交快照。"""
    return SubscriptionDownloadSnapshot(
        idempotency_key=record.idempotency_key,
        subscription_id=record.subscription_id,
        task_id=record.task_id,
        state=record.state,
        attempt_count=record.attempt_count,
        attempt_token=record.attempt_token,
        downloader=record.downloader,
        download_hash=record.download_hash,
        available_at=record.available_at,
        last_error=record.last_error,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


class TransactionalSubscriptionDownloadRepository:
    """为每次提交认领和状态变更创建独立短事务。"""

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        """保存由组合根提供的同步 Session 工厂。"""
        self._session_factory = session_factory

    def _read(self, operation: Callable[[SubscriptionDownloadOper], T]) -> T:
        """在短 Session 中执行一次只读查询。"""
        with self._session_factory() as session:
            return operation(SubscriptionDownloadOper(session))

    def _write(self, operation: Callable[[SubscriptionDownloadOper], T]) -> T:
        """在显式短事务中执行一次状态变更。"""
        with self._session_factory() as session:
            unit_of_work = SqlAlchemyUnitOfWork(session)
            try:
                result = operation(SubscriptionDownloadOper(session))
                unit_of_work.commit()
                return result
            except Exception:
                unit_of_work.rollback()
                raise

    def claim(self, request: SubscriptionDownloadRequest) -> SubscriptionDownloadClaim:
        """认领唯一提交权并返回脱离 Session 的当前状态。"""
        def operation(repository: SubscriptionDownloadOper) -> SubscriptionDownloadClaim:
            """在同一事务内完成唯一插入或 fenced 重领。"""
            record, acquired = repository.claim(request)
            return SubscriptionDownloadClaim(acquired=acquired, snapshot=_snapshot(record))

        return self._write(operation)

    def get(self, idempotency_key: str) -> Optional[SubscriptionDownloadSnapshot]:
        """读取一个已存在的提交快照。"""
        return self._read(
            lambda repository: (
                _snapshot(record)
                if (record := repository.get(idempotency_key)) is not None
                else None
            )
        )

    def mark_accepted(
        self,
        *,
        idempotency_key: str,
        attempt_token: str,
        downloader: Optional[str],
        download_hash: str,
    ) -> bool:
        """记录下载器已接受任务。"""
        return self._write(lambda repository: repository.mark_accepted(
            idempotency_key=idempotency_key,
            attempt_token=attempt_token,
            downloader=downloader,
            download_hash=download_hash,
        ))

    def mark_succeeded(self, *, idempotency_key: str, attempt_token: str) -> bool:
        """记录 canonical 本地结算已完成。"""
        return self._write(lambda repository: repository.mark_succeeded(
            idempotency_key=idempotency_key,
            attempt_token=attempt_token,
        ))

    def mark_retryable(
        self,
        *,
        idempotency_key: str,
        attempt_token: str,
        available_at: str,
        error: Optional[str],
    ) -> bool:
        """记录未产生外部副作用的延迟重试状态。"""
        return self._write(lambda repository: repository.mark_retryable(
            idempotency_key=idempotency_key,
            attempt_token=attempt_token,
            available_at=available_at,
            error=error,
        ))

    def mark_reconcile_required(
        self,
        *,
        idempotency_key: str,
        attempt_token: str,
        error: Optional[str],
        downloader: Optional[str] = None,
        download_hash: Optional[str] = None,
    ) -> bool:
        """冻结可能已被下载器接受的提交。"""
        return self._write(lambda repository: repository.mark_reconcile_required(
            idempotency_key=idempotency_key,
            attempt_token=attempt_token,
            error=error,
            downloader=downloader,
            download_hash=download_hash,
        ))

    def mark_cancelled(self, *, idempotency_key: str, attempt_token: str) -> bool:
        """在外部提交前收口取消。"""
        return self._write(lambda repository: repository.mark_cancelled(
            idempotency_key=idempotency_key,
            attempt_token=attempt_token,
        ))

    def has_started_for_task(self, task_id: str) -> bool:
        """查询搜索任务是否已越过可安全取消边界。"""
        return self._read(lambda repository: repository.has_started_for_task(task_id))
