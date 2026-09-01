"""订阅下载提交账本的事务内状态转换。"""

from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.application.download.admission import SubscriptionDownloadRequest
from app.db.base import DbOper, execute_dml
from app.db.models.subscriptiondownload import SubscriptionDownloadSubmission


def utc_now_text() -> str:
    """返回可按字符串稳定排序的 UTC ISO 时间。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SubscriptionDownloadOper(DbOper):
    """在调用方事务内维护唯一下载提交及其 fenced 终态。"""

    def claim(self, request: SubscriptionDownloadRequest) -> tuple[SubscriptionDownloadSubmission, bool]:
        """创建或原子重领允许重试的提交记录。"""
        if not isinstance(self._db, Session):
            raise RuntimeError("订阅下载提交认领需要调用方提供同步 Session")
        now = utc_now_text()
        attempt_token = uuid4().hex
        record = self._db.execute(
            select(SubscriptionDownloadSubmission).where(
                SubscriptionDownloadSubmission.idempotency_key == request.idempotency_key
            )
        ).scalars().first()
        if record is None:
            try:
                with self._db.begin_nested():
                    record = SubscriptionDownloadSubmission(
                        idempotency_key=request.idempotency_key,
                        subscription_id=request.subscription_id,
                        task_id=request.task_id,
                        logical_identity=request.logical_identity,
                        resource_key=request.resource_key,
                        coverage=request.coverage,
                        mode=request.mode,
                        delivery_scope=request.delivery_scope,
                        state="submitting",
                        attempt_count=1,
                        attempt_token=attempt_token,
                        created_at=now,
                        updated_at=now,
                        started_at=now,
                    )
                    self._db.add(record)
                    self._db.flush()
                return record, True
            except IntegrityError:
                self._db.expire_all()
                record = self._db.execute(
                    select(SubscriptionDownloadSubmission).where(
                        SubscriptionDownloadSubmission.idempotency_key == request.idempotency_key
                    )
                ).scalar_one()

        reclaimable = record.state in {"retryable", "cancelled"} and (
            record.available_at is None or record.available_at <= now
        )
        if not reclaimable:
            return record, False
        updated = execute_dml(
            self._db,
            update(SubscriptionDownloadSubmission)
            .where(
                SubscriptionDownloadSubmission.id == record.id,
                SubscriptionDownloadSubmission.state.in_(("retryable", "cancelled")),
                or_(
                    SubscriptionDownloadSubmission.available_at.is_(None),
                    SubscriptionDownloadSubmission.available_at <= now,
                ),
            )
            .values(
                state="submitting",
                task_id=request.task_id,
                attempt_count=SubscriptionDownloadSubmission.attempt_count + 1,
                attempt_token=attempt_token,
                downloader=None,
                download_hash=None,
                available_at=None,
                last_error=None,
                updated_at=now,
                started_at=now,
                finished_at=None,
            ),
            execution_options={"synchronize_session": False},
        )
        self._db.flush()
        self._db.expire_all()
        current = self._db.execute(
            select(SubscriptionDownloadSubmission).where(
                SubscriptionDownloadSubmission.id == record.id
            )
        ).scalar_one()
        return current, bool(updated and current.attempt_token == attempt_token)

    def get(self, idempotency_key: str) -> Optional[SubscriptionDownloadSubmission]:
        """按稳定幂等键读取提交记录。"""
        if not isinstance(self._db, Session):
            raise RuntimeError("订阅下载提交查询需要调用方提供同步 Session")
        return self._db.execute(
            select(SubscriptionDownloadSubmission).where(
                SubscriptionDownloadSubmission.idempotency_key == idempotency_key
            )
        ).scalars().first()

    def mark_accepted(
        self,
        *,
        idempotency_key: str,
        attempt_token: str,
        downloader: Optional[str],
        download_hash: str,
    ) -> bool:
        """把当前 submitting 尝试推进为下载器已接受。"""
        return self._transition(
            idempotency_key=idempotency_key,
            attempt_token=attempt_token,
            source_states=("submitting",),
            values={
                "state": "accepted",
                "downloader": downloader,
                "download_hash": download_hash,
            },
        )

    def mark_succeeded(self, *, idempotency_key: str, attempt_token: str) -> bool:
        """把当前 accepted 尝试推进为本地结算成功。"""
        return self._transition(
            idempotency_key=idempotency_key,
            attempt_token=attempt_token,
            source_states=("accepted",),
            values={"state": "succeeded", "finished_at": utc_now_text()},
        )

    def mark_retryable(
        self,
        *,
        idempotency_key: str,
        attempt_token: str,
        available_at: str,
        error: Optional[str],
    ) -> bool:
        """把下载器明确拒绝的当前尝试收口为延迟可重试。"""
        return self._transition(
            idempotency_key=idempotency_key,
            attempt_token=attempt_token,
            source_states=("submitting",),
            values={
                "state": "retryable",
                "available_at": available_at,
                "last_error": error,
                "finished_at": utc_now_text(),
            },
        )

    def mark_reconcile_required(
        self,
        *,
        idempotency_key: str,
        attempt_token: str,
        error: Optional[str],
        downloader: Optional[str],
        download_hash: Optional[str],
    ) -> bool:
        """冻结 submitting/accepted 尝试，禁止自动重新提交。"""
        values: dict[str, object] = {
            "state": "reconcile_required",
            "last_error": error,
            "finished_at": utc_now_text(),
        }
        if downloader is not None:
            values["downloader"] = downloader
        if download_hash is not None:
            values["download_hash"] = download_hash
        return self._transition(
            idempotency_key=idempotency_key,
            attempt_token=attempt_token,
            source_states=("submitting", "accepted"),
            values=values,
        )

    def mark_cancelled(self, *, idempotency_key: str, attempt_token: str) -> bool:
        """在外部提交前把当前尝试收口为已取消。"""
        return self._transition(
            idempotency_key=idempotency_key,
            attempt_token=attempt_token,
            source_states=("submitting",),
            values={"state": "cancelled", "finished_at": utc_now_text()},
        )

    def has_started_for_task(self, task_id: str) -> bool:
        """判断任务是否进入过外部副作用边界或已成功。"""
        if not isinstance(self._db, Session):
            raise RuntimeError("订阅下载提交查询需要调用方提供同步 Session")
        return bool(self._db.execute(
            select(SubscriptionDownloadSubmission.id)
            .where(
                SubscriptionDownloadSubmission.task_id == task_id,
                SubscriptionDownloadSubmission.state.in_((
                    "submitting",
                    "accepted",
                    "succeeded",
                    "reconcile_required",
                )),
            )
            .limit(1)
        ).scalar())

    def _transition(
        self,
        *,
        idempotency_key: str,
        attempt_token: str,
        source_states: tuple[str, ...],
        values: dict[str, object],
    ) -> bool:
        """以当前尝试令牌原子推进状态，拒绝过期执行者写回。"""
        if not isinstance(self._db, Session):
            raise RuntimeError("订阅下载提交写入需要调用方提供同步 Session")
        now = utc_now_text()
        return bool(execute_dml(
            self._db,
            update(SubscriptionDownloadSubmission)
            .where(
                and_(
                    SubscriptionDownloadSubmission.idempotency_key == idempotency_key,
                    SubscriptionDownloadSubmission.attempt_token == attempt_token,
                    SubscriptionDownloadSubmission.state.in_(source_states),
                )
            )
            .values(updated_at=now, **values),
            execution_options={"synchronize_session": False},
        ))
