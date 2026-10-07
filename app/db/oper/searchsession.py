"""搜索检查点行的版本 CAS，由适配器拥有事务。"""

from datetime import datetime, timezone
from typing import Any, Optional, cast

from sqlalchemy import delete, exists, literal, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
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

    def _lease_held(self, task_id: str, task_lease: Optional[str]) -> Any:
        """任务租约仍属于调用方的条件；与写入放在同一条语句里，避免校验后发生交接。"""
        return exists().where(
            SubscriptionSearchTask.task_id == task_id,
            SubscriptionSearchTask.lease_token == task_lease,
            SubscriptionSearchTask.lease_expires_at > self._now(),
            SubscriptionSearchTask.cancel_requested == 0,
        )

    def create(self, task_id: str, payload: str, task_lease: Optional[str]) -> Optional[SearchSession]:
        """暂存初始检查点，不提交调用方事务；租约条件与插入在同一条语句内校验。

        调用方读取时检查点为空，之后若已有同任务行，只可能是失去租约的旧执行者迟到写入。
        当前租约持有者以版本递增接管该行，旧执行者的版本快照随之失效；未持有租约时不写入。
        """
        dialect = self._db.get_bind().dialect.name
        if dialect not in {"postgresql", "sqlite"}:
            raise RuntimeError(f"搜索检查点不支持数据库方言：{dialect}")
        insert = pg_insert if dialect == "postgresql" else sqlite_insert
        now = self._now()
        source = select(literal(task_id), literal(0), literal(payload), literal(now)).where(
            self._lease_held(task_id, task_lease))
        statement = insert(SearchSession).from_select(
            ["task_id", "version", "payload", "updated_at"], source,
        ).on_conflict_do_nothing(index_elements=[SearchSession.task_id])
        if not self._db.execute(statement).rowcount:
            taken = self._db.execute(update(SearchSession).where(
                SearchSession.task_id == task_id, self._lease_held(task_id, task_lease),
            ).values(payload=payload, version=SearchSession.version + 1, updated_at=now))
            if not taken.rowcount:
                return None
        self._db.expire_all()
        return self.get(task_id)

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

    def purge_stale(self, before: str) -> None:
        """保底删除长期未更新的检查点；任务终态通常已删除，这里只回收异常遗留。"""
        self._db.execute(delete(SearchSession).where(SearchSession.updated_at < before))

    def delete(self, task_id: str, version: int, task_lease: Optional[str]) -> None:
        """本轮结束后删除检查点；版本与租约在同一语句内校验，迟到 worker 不能删除新进度。"""
        self._db.execute(delete(SearchSession).where(
            SearchSession.task_id == task_id, SearchSession.version == version,
            self._lease_held(task_id, task_lease),
        ))
