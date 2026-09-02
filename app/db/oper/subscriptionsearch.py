"""订阅搜索批次与任务的持久队列读写。"""

from datetime import datetime, timedelta, timezone
from typing import Mapping, Optional
from uuid import uuid4

from sqlalchemy import and_, case, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.base import DbOper, execute_dml
from app.db.models.subscriptionsearch import (
    SubscriptionSearchBatch,
    SubscriptionSearchTask,
    SubscriptionSiteBudget,
)


def utc_now_text() -> str:
    """返回可按字符串稳定排序的 UTC ISO 时间。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SubscriptionSearchOper(DbOper):
    """在调用方事务中维护搜索队列、租约和批次聚合。"""

    def enqueue(
        self,
        *,
        subscription_ids: tuple[int, ...],
        source: str,
        priority: int,
        available_at_by_subscription: Optional[Mapping[int, str]],
    ) -> tuple[SubscriptionSearchBatch, int, int]:
        """创建批次，并以活动键合并同一订阅的重叠搜索入口。"""
        if not isinstance(self._db, Session):
            raise RuntimeError("订阅搜索入队需要调用方提供同步 Session")
        now = utc_now_text()
        batch = SubscriptionSearchBatch(
            batch_id=uuid4().hex,
            source=source,
            state="queued",
            priority=priority,
            total_count=0,
            created_at=now,
            updated_at=now,
        )
        self._db.add(batch)
        self._db.flush()
        created = 0
        coalesced = 0
        for position, subscription_id in enumerate(dict.fromkeys(subscription_ids)):
            active_key = f"subscription:{subscription_id}"
            available_at = (
                available_at_by_subscription.get(subscription_id, now)
                if available_at_by_subscription
                else now
            )
            task = SubscriptionSearchTask(
                task_id=uuid4().hex,
                batch_id=batch.batch_id,
                subscription_id=subscription_id,
                active_key=active_key,
                source=source,
                priority=priority,
                position=position,
                state="queued",
                phase="queued",
                available_at=available_at,
                created_at=now,
                updated_at=now,
            )
            try:
                with self._db.begin_nested():
                    self._db.add(task)
                    self._db.flush()
                created += 1
            except IntegrityError:
                coalesced += 1
                execute_dml(
                    self._db,
                    update(SubscriptionSearchTask)
                    .where(SubscriptionSearchTask.active_key == active_key)
                    .values(
                        priority=case(
                            (SubscriptionSearchTask.priority < priority, priority),
                            else_=SubscriptionSearchTask.priority,
                        ),
                        available_at=case(
                            (
                                or_(
                                    SubscriptionSearchTask.available_at.is_(None),
                                    SubscriptionSearchTask.available_at > available_at,
                                ),
                                available_at,
                            ),
                            else_=SubscriptionSearchTask.available_at,
                        ),
                        updated_at=now,
                    ),
                    execution_options={"synchronize_session": False},
                )
        batch.total_count = created
        if created == 0:
            batch.state = "completed"
            batch.finished_at = now
        return batch, created, coalesced

    def claim_next(self, *, owner: str, lease_seconds: int) -> Optional[SubscriptionSearchTask]:
        """使用 CAS 认领最高优先级任务，过期 running 任务可被恢复。"""
        if not isinstance(self._db, Session):
            raise RuntimeError("订阅搜索认领需要调用方提供同步 Session")
        now = utc_now_text()
        fairness_before = (
            datetime.now(timezone.utc) - timedelta(minutes=15)
        ).isoformat(timespec="seconds")
        lease_expires_at = (
            datetime.now(timezone.utc) + timedelta(seconds=max(1, lease_seconds))
        ).isoformat(timespec="seconds")
        for _attempt in range(5):
            candidate = self._db.execute(
                select(SubscriptionSearchTask)
                .where(
                    SubscriptionSearchTask.cancel_requested == 0,
                    or_(
                        SubscriptionSearchTask.available_at.is_(None),
                        SubscriptionSearchTask.available_at <= now,
                    ),
                    or_(
                        SubscriptionSearchTask.state == "queued",
                        and_(
                            SubscriptionSearchTask.state == "running",
                            or_(
                                SubscriptionSearchTask.lease_expires_at.is_(None),
                                SubscriptionSearchTask.lease_expires_at <= now,
                            ),
                        ),
                    ),
                )
                .order_by(
                    case(
                        (SubscriptionSearchTask.created_at <= fairness_before, 1),
                        else_=0,
                    ).desc(),
                    SubscriptionSearchTask.priority.desc(),
                    SubscriptionSearchTask.available_at.asc(),
                    SubscriptionSearchTask.created_at.asc(),
                    SubscriptionSearchTask.position.asc(),
                    SubscriptionSearchTask.id.asc(),
                )
                .limit(1)
            ).scalars().first()
            if candidate is None:
                return None
            lease_token = uuid4().hex
            claimed = execute_dml(
                self._db,
                update(SubscriptionSearchTask)
                .where(
                    SubscriptionSearchTask.id == candidate.id,
                    SubscriptionSearchTask.cancel_requested == 0,
                    or_(
                        SubscriptionSearchTask.available_at.is_(None),
                        SubscriptionSearchTask.available_at <= now,
                    ),
                    or_(
                        SubscriptionSearchTask.state == "queued",
                        and_(
                            SubscriptionSearchTask.state == "running",
                            or_(
                                SubscriptionSearchTask.lease_expires_at.is_(None),
                                SubscriptionSearchTask.lease_expires_at <= now,
                            ),
                        ),
                    ),
                )
                .values(
                    state="running",
                    phase="matching",
                    current_site_id=None,
                    lease_owner=owner,
                    lease_token=lease_token,
                    lease_expires_at=lease_expires_at,
                    attempt_count=SubscriptionSearchTask.attempt_count + 1,
                    started_at=func.coalesce(SubscriptionSearchTask.started_at, now),
                    updated_at=now,
                ),
                execution_options={"synchronize_session": False},
            )
            if not claimed:
                self._db.expire_all()
                continue
            execute_dml(
                self._db,
                update(SubscriptionSearchBatch)
                .where(
                    SubscriptionSearchBatch.batch_id == candidate.batch_id,
                    SubscriptionSearchBatch.state.in_(("queued", "running")),
                )
                .values(
                    state="running",
                    started_at=func.coalesce(SubscriptionSearchBatch.started_at, now),
                    updated_at=now,
                ),
                execution_options={"synchronize_session": False},
            )
            self._db.flush()
            self._db.expire_all()
            claimed_task: Optional[SubscriptionSearchTask] = self._db.execute(
                select(SubscriptionSearchTask).where(
                    SubscriptionSearchTask.id == candidate.id,
                    SubscriptionSearchTask.lease_token == lease_token,
                )
            ).scalars().first()
            return claimed_task
        return None

    def update_task_phase(
        self,
        *,
        task_id: str,
        lease_token: str,
        phase: str,
        current_site_id: Optional[int],
    ) -> bool:
        """只允许当前运行租约推进用户可见阶段。"""
        if not isinstance(self._db, Session):
            raise RuntimeError("订阅搜索阶段更新需要调用方提供同步 Session")
        task = self._db.execute(
            select(SubscriptionSearchTask).where(
                SubscriptionSearchTask.task_id == task_id,
                SubscriptionSearchTask.state == "running",
                SubscriptionSearchTask.lease_token == lease_token,
            )
        ).scalars().first()
        if task is None:
            return False
        now = utc_now_text()
        updated = execute_dml(
            self._db,
            update(SubscriptionSearchTask)
            .where(
                SubscriptionSearchTask.id == task.id,
                SubscriptionSearchTask.state == "running",
                SubscriptionSearchTask.lease_token == lease_token,
            )
            .values(
                phase=phase,
                current_site_id=current_site_id,
                updated_at=now,
            ),
            execution_options={"synchronize_session": False},
        )
        if updated:
            execute_dml(
                self._db,
                update(SubscriptionSearchBatch)
                .where(SubscriptionSearchBatch.batch_id == task.batch_id)
                .values(updated_at=now),
                execution_options={"synchronize_session": False},
            )
        return bool(updated)

    def finish_task(
        self,
        *,
        task_id: str,
        lease_token: str,
        state: str,
        error: Optional[str],
    ) -> bool:
        """以当前租约令牌收口任务，并重新计算批次终态。"""
        if not isinstance(self._db, Session):
            raise RuntimeError("订阅搜索收口需要调用方提供同步 Session")
        if state not in {"completed", "failed", "cancelled", "skipped"}:
            raise ValueError(f"不支持的订阅搜索终态：{state}")
        task = self._db.execute(
            select(SubscriptionSearchTask).where(
                SubscriptionSearchTask.task_id == task_id,
                SubscriptionSearchTask.state == "running",
                SubscriptionSearchTask.lease_token == lease_token,
            )
        ).scalars().first()
        if task is None:
            return False
        now = utc_now_text()
        updated = execute_dml(
            self._db,
            update(SubscriptionSearchTask)
            .where(
                SubscriptionSearchTask.id == task.id,
                SubscriptionSearchTask.state == "running",
                SubscriptionSearchTask.lease_token == lease_token,
            )
            .values(
                state=state,
                phase=state,
                current_site_id=None,
                active_key=None,
                lease_owner=None,
                lease_token=None,
                lease_expires_at=None,
                updated_at=now,
                finished_at=now,
                last_error=error,
            ),
            execution_options={"synchronize_session": False},
        )
        if not updated:
            return False
        self._refresh_batch(task.batch_id, now=now, error=error)
        return True

    def release_task(
        self,
        *,
        task_id: str,
        lease_token: str,
        cancelled: bool,
    ) -> bool:
        """取消时收口，停机时把任务退回队列并保留稳定游标。"""
        if not isinstance(self._db, Session):
            raise RuntimeError("订阅搜索释放需要调用方提供同步 Session")
        task = self._db.execute(
            select(SubscriptionSearchTask).where(
                SubscriptionSearchTask.task_id == task_id,
                SubscriptionSearchTask.state == "running",
                SubscriptionSearchTask.lease_token == lease_token,
            )
        ).scalars().first()
        if task is None:
            return False
        should_cancel = cancelled or bool(task.cancel_requested) or self._batch_cancel_requested(task.batch_id)
        if should_cancel:
            return self.finish_task(
                task_id=task_id,
                lease_token=lease_token,
                state="cancelled",
                error=None,
            )
        now = utc_now_text()
        updated = execute_dml(
            self._db,
            update(SubscriptionSearchTask)
            .where(
                SubscriptionSearchTask.id == task.id,
                SubscriptionSearchTask.state == "running",
                SubscriptionSearchTask.lease_token == lease_token,
            )
            .values(
                state="queued",
                phase="queued",
                current_site_id=None,
                lease_owner=None,
                lease_token=None,
                lease_expires_at=None,
                updated_at=now,
            ),
            execution_options={"synchronize_session": False},
        )
        if not updated:
            return False
        self._refresh_batch(task.batch_id, now=now, error=None)
        return True

    def is_cancel_requested(self, task_id: str) -> bool:
        """读取任务和批次取消标记。"""
        if not isinstance(self._db, Session):
            raise RuntimeError("订阅搜索取消查询需要调用方提供同步 Session")
        task = self._db.execute(
            select(SubscriptionSearchTask).where(SubscriptionSearchTask.task_id == task_id)
        ).scalars().first()
        return bool(task and (task.cancel_requested or self._batch_cancel_requested(task.batch_id)))

    def request_cancel(self, batch_id: str) -> bool:
        """标记批次取消，并立即收口尚未发出的排队任务。"""
        if not isinstance(self._db, Session):
            raise RuntimeError("订阅搜索取消需要调用方提供同步 Session")
        batch = self._db.execute(
            select(SubscriptionSearchBatch).where(SubscriptionSearchBatch.batch_id == batch_id)
        ).scalars().first()
        if batch is None or batch.state in {"completed", "failed", "cancelled"}:
            return False
        now = utc_now_text()
        execute_dml(
            self._db,
            update(SubscriptionSearchBatch)
            .where(SubscriptionSearchBatch.id == batch.id)
            .values(cancel_requested=1, state="cancelling", updated_at=now),
            execution_options={"synchronize_session": False},
        )
        execute_dml(
            self._db,
            update(SubscriptionSearchTask)
            .where(
                SubscriptionSearchTask.batch_id == batch_id,
                SubscriptionSearchTask.state == "queued",
            )
            .values(
                state="cancelled",
                phase="cancelled",
                current_site_id=None,
                active_key=None,
                cancel_requested=1,
                finished_at=now,
                updated_at=now,
            ),
            execution_options={"synchronize_session": False},
        )
        execute_dml(
            self._db,
            update(SubscriptionSearchTask)
            .where(
                SubscriptionSearchTask.batch_id == batch_id,
                SubscriptionSearchTask.state == "running",
            )
            .values(cancel_requested=1, phase="cancelling", updated_at=now),
            execution_options={"synchronize_session": False},
        )
        self._refresh_batch(batch_id, now=now, error=None)
        return True

    def get_batch(self, batch_id: str) -> Optional[SubscriptionSearchBatch]:
        """按稳定批次 ID 读取聚合记录。"""
        if not isinstance(self._db, Session):
            raise RuntimeError("订阅搜索批次查询需要调用方提供同步 Session")
        batch: Optional[SubscriptionSearchBatch] = self._db.execute(
            select(SubscriptionSearchBatch).where(SubscriptionSearchBatch.batch_id == batch_id)
        ).scalars().first()
        return batch

    def claim_site(
        self,
        *,
        site_id: int,
        owner: str,
        lease_seconds: int,
    ) -> tuple[SubscriptionSiteBudget, bool]:
        """以 CAS 认领单站点租约，返回当前或已认领预算记录。"""
        if not isinstance(self._db, Session):
            raise RuntimeError("订阅站点预算认领需要调用方提供同步 Session")
        now = utc_now_text()
        record = self._ensure_site_budget(site_id=site_id, now=now)
        lease_busy = bool(
            record.lease_token
            and record.lease_expires_at
            and record.lease_expires_at > now
        )
        cooldown_active = bool(
            record.last_outcome not in {None, "success", "skipped"}
            and record.next_allowed_at > now
        )
        if lease_busy or cooldown_active:
            return record, False
        lease_token = uuid4().hex
        lease_expires_at = (
            datetime.now(timezone.utc) + timedelta(seconds=max(1, lease_seconds))
        ).isoformat(timespec="seconds")
        updated = execute_dml(
            self._db,
            update(SubscriptionSiteBudget)
            .where(
                SubscriptionSiteBudget.id == record.id,
                or_(
                    SubscriptionSiteBudget.lease_token.is_(None),
                    SubscriptionSiteBudget.lease_expires_at.is_(None),
                    SubscriptionSiteBudget.lease_expires_at <= now,
                ),
                or_(
                    SubscriptionSiteBudget.next_allowed_at <= now,
                    SubscriptionSiteBudget.last_outcome.is_(None),
                    SubscriptionSiteBudget.last_outcome.in_(("success", "skipped")),
                ),
            )
            .values(
                lease_owner=owner,
                lease_token=lease_token,
                lease_expires_at=lease_expires_at,
                next_allowed_at=now,
                updated_at=now,
            ),
            execution_options={"synchronize_session": False},
        )
        if not updated:
            self._db.expire_all()
            current = self._db.execute(
                select(SubscriptionSiteBudget).where(
                    SubscriptionSiteBudget.site_id == site_id
                )
            ).scalar_one()
            return current, False
        self._db.flush()
        self._db.expire_all()
        claimed = self._db.execute(
            select(SubscriptionSiteBudget).where(
                SubscriptionSiteBudget.site_id == site_id,
                SubscriptionSiteBudget.lease_token == lease_token,
            )
        ).scalar_one()
        return claimed, True

    def finish_site(
        self,
        *,
        site_id: int,
        lease_token: str,
        outcome: str,
        next_allowed_at: str,
        error: Optional[str],
    ) -> bool:
        """释放当前站点租约，并按结果推进失败或恢复计数。"""
        if not isinstance(self._db, Session):
            raise RuntimeError("订阅站点预算收口需要调用方提供同步 Session")
        record = self._db.execute(
            select(SubscriptionSiteBudget).where(
                SubscriptionSiteBudget.site_id == site_id,
                SubscriptionSiteBudget.lease_token == lease_token,
            )
        ).scalars().first()
        if record is None:
            return False
        if outcome == "success":
            failures = max(0, record.consecutive_failures - 1)
            success_streak = record.success_streak + 1
        elif outcome == "skipped":
            failures = record.consecutive_failures
            success_streak = record.success_streak
        else:
            failures = record.consecutive_failures + 1
            success_streak = 0
        now = utc_now_text()
        return bool(execute_dml(
            self._db,
            update(SubscriptionSiteBudget)
            .where(
                SubscriptionSiteBudget.id == record.id,
                SubscriptionSiteBudget.lease_token == lease_token,
            )
            .values(
                lease_owner=None,
                lease_token=None,
                lease_expires_at=None,
                next_allowed_at=next_allowed_at,
                consecutive_failures=failures,
                success_streak=success_streak,
                last_outcome=outcome,
                last_error=error,
                updated_at=now,
            ),
            execution_options={"synchronize_session": False},
        ))

    def _ensure_site_budget(self, *, site_id: int, now: str) -> SubscriptionSiteBudget:
        """并发安全地创建站点预算初始记录。"""
        db = self._db
        if not isinstance(db, Session):
            raise RuntimeError("订阅站点预算初始化需要调用方提供同步 Session")
        record: Optional[SubscriptionSiteBudget] = db.execute(
            select(SubscriptionSiteBudget).where(
                SubscriptionSiteBudget.site_id == site_id
            )
        ).scalars().first()
        if record is not None:
            return record
        try:
            with db.begin_nested():
                db.add(SubscriptionSiteBudget(
                    site_id=site_id,
                    next_allowed_at=now,
                    updated_at=now,
                ))
                db.flush()
        except IntegrityError:
            db.expire_all()
        created: SubscriptionSiteBudget = db.execute(
            select(SubscriptionSiteBudget).where(
                SubscriptionSiteBudget.site_id == site_id
            )
        ).scalar_one()
        return created

    def _batch_cancel_requested(self, batch_id: str) -> bool:
        """在当前事务中读取批次取消标记。"""
        db = self._db
        if not isinstance(db, Session):
            raise RuntimeError("订阅搜索批次取消查询需要调用方提供同步 Session")
        return bool(db.execute(
            select(SubscriptionSearchBatch.cancel_requested).where(
                SubscriptionSearchBatch.batch_id == batch_id
            )
        ).scalar())

    def _refresh_batch(self, batch_id: str, *, now: str, error: Optional[str]) -> None:
        """依据所属任务终态重新计算批次计数和聚合状态。"""
        db = self._db
        if not isinstance(db, Session):
            raise RuntimeError("订阅搜索批次刷新需要调用方提供同步 Session")
        rows = db.execute(
            select(SubscriptionSearchTask.state, func.count())  # pylint: disable=not-callable
            .where(SubscriptionSearchTask.batch_id == batch_id)
            .group_by(SubscriptionSearchTask.state)
        ).all()
        counts = {state: int(count) for state, count in rows}
        completed = counts.get("completed", 0)
        failed = counts.get("failed", 0)
        cancelled = counts.get("cancelled", 0)
        skipped = counts.get("skipped", 0)
        terminal = completed + failed + cancelled + skipped
        batch = self.get_batch(batch_id)
        if batch is None:
            return
        if terminal >= batch.total_count:
            if failed:
                state = "failed"
            elif cancelled or batch.cancel_requested:
                state = "cancelled"
            elif skipped:
                state = "skipped"
            else:
                state = "completed"
            finished_at = now
        else:
            if batch.cancel_requested:
                state = "cancelling"
            elif counts.get("running", 0):
                state = "running"
            else:
                state = "queued"
            finished_at = None
        execute_dml(
            self._db,
            update(SubscriptionSearchBatch)
            .where(SubscriptionSearchBatch.id == batch.id)
            .values(
                state=state,
                finished_count=completed,
                failed_count=failed,
                cancelled_count=cancelled,
                skipped_count=skipped,
                updated_at=now,
                finished_at=finished_at,
                last_error=error or batch.last_error,
            ),
            execution_options={"synchronize_session": False},
        )
