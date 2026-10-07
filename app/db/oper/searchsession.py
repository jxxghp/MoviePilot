"""搜索检查点行的版本 CAS，由适配器拥有事务。"""

from datetime import datetime, timezone
from typing import Any, Optional, cast

from sqlalchemy import delete, exists, select, update
from sqlalchemy.orm import Session

from app.db.models.searchsession import SearchSession
from app.db.models.subscriptionsearch import SubscriptionSearchTask


class SearchSessionOper:
    """所有变更使用调用方 Session；写入前核对队列任务租约仍属于调用方。"""

    def __init__(self, session: Session) -> None:
        self._db = session

    def get(self, task_id: str) -> Optional[SearchSession]:
        """在当前事务中读取任务检查点。"""
        return cast(Optional[SearchSession], self._db.scalar(
            select(SearchSession).where(SearchSession.task_id == task_id)))

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    def _task_active(self, task_id: str, task_lease: Optional[str]) -> bool:
        return bool(self._db.scalar(select(self._lease_held(task_id, task_lease))))

    def _lease_held(self, task_id: str, task_lease: Optional[str]) -> Any:
        """任务租约仍属于调用方的条件；与写入放在同一条语句里，避免校验后发生交接。"""
        return exists().where(
            SubscriptionSearchTask.task_id == task_id,
            SubscriptionSearchTask.lease_token == task_lease,
            SubscriptionSearchTask.lease_expires_at > self._now(),
            SubscriptionSearchTask.cancel_requested == 0,
        )

    def create(self, task_id: str, payload: str, task_lease: Optional[str]) -> Optional[SearchSession]:
        """暂存初始检查点，不提交调用方事务。"""
        if not self._task_active(task_id, task_lease):
            return None
        row = SearchSession(task_id=task_id, payload=payload, version=0, updated_at=self._now())
        self._db.add(row)
        self._db.flush()
        return row

    def save(self, task_id: str, version: int, payload: str, task_lease: Optional[str]) -> Optional[SearchSession]:
        """候选与游标是同一 payload；仅持有任务租约且版本未变的 worker 能推进。"""
        result = self._db.execute(update(SearchSession).where(
            SearchSession.task_id == task_id, SearchSession.version == version,
            self._lease_held(task_id, task_lease),
        ).values(payload=payload, version=version + 1, updated_at=self._now()))
        if not result.rowcount:
            return None
        self._db.expire_all()
        return self.get(task_id)

    def delete(self, task_id: str, version: int, task_lease: Optional[str]) -> None:
        """本轮结束后删除检查点；版本与租约在同一语句内校验，迟到 worker 不能删除新进度。"""
        self._db.execute(delete(SearchSession).where(
            SearchSession.task_id == task_id, SearchSession.version == version,
            self._lease_held(task_id, task_lease),
        ))
