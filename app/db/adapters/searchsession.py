"""搜索检查点 Port 的同步短事务实现。"""

from collections.abc import Callable
from typing import Optional, TypeVar

from sqlalchemy.orm import Session

from app.application.search.session import SearchSessionSnapshot
from app.db.models.searchsession import SearchSession
from app.db.oper.searchsession import SearchSessionOper
from app.db.uow import SqlAlchemyUnitOfWork

T = TypeVar("T")


def _snapshot(row: Optional[SearchSession]) -> Optional[SearchSessionSnapshot]:
    """在 Session 关闭前复制字符串快照，不返回 ORM 行。"""
    if row is None:
        return None
    return SearchSessionSnapshot(row.task_id, row.version, row.payload)


class TransactionalSearchSessionRepository:
    """每次保存只拥有一个短事务，不跨站点请求持有 Session。"""

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self._session_factory = session_factory

    def _write(self, operation: Callable[[SearchSessionOper], T]) -> T:
        with self._session_factory() as session:
            transaction = SqlAlchemyUnitOfWork(session)
            try:
                result = operation(SearchSessionOper(session))
                transaction.commit()
                return result
            except Exception:
                transaction.rollback()
                raise

    def get(self, *, task_id: str) -> Optional[SearchSessionSnapshot]:
        """只读操作不创建隐式写事务。"""
        with self._session_factory() as session:
            return _snapshot(SearchSessionOper(session).get(task_id))

    def create(self, *, task_id: str, payload: str, task_lease: Optional[str]) -> Optional[SearchSessionSnapshot]:
        """创建任务检查点，失去任务租约时返回空。"""
        return self._write(lambda oper: _snapshot(oper.create(task_id, payload, task_lease)))

    def save(self, *, snapshot: SearchSessionSnapshot, payload: str,
             task_lease: Optional[str]) -> Optional[SearchSessionSnapshot]:
        """原子推进页快照并返回新版本，失去所有权时返回空。"""
        return self._write(lambda oper: _snapshot(oper.save(snapshot.task_id, snapshot.version, payload, task_lease)))

    def delete(self, *, snapshot: SearchSessionSnapshot, task_lease: Optional[str]) -> None:
        """删除已结束任务的检查点，仅当仍是自己保存的版本。"""
        self._write(lambda oper: oper.delete(snapshot.task_id, snapshot.version, task_lease))
