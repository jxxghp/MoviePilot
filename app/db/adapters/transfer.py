"""整理任务持久准入端口的 SQLAlchemy 适配器。"""

from collections.abc import Callable
from datetime import datetime
from uuid import uuid4

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.application.transfer import (
    TRANSFER_ADMISSION_ACCEPTED,
    TransferAdmission,
)
from app.db.models.transferpending import TransferPending
from app.db.oper.transferpending import TransferPendingOper
from app.db.uow import SqlAlchemyUnitOfWork


class TransactionalTransferAdmissionRepository:
    """以短生命周期 Session 实现整理任务持久准入端口。"""

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        """保存由组合根提供的同步会话工厂。"""
        self._session_factory = session_factory

    @staticmethod
    def _now() -> str:
        """生成与历史登记时间可按字典序比较的当前时间。"""
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def _project(pending: TransferPending) -> TransferAdmission:
        """在 Session 有效期内把 ORM 行冻结为应用层 DTO。"""
        created_at = pending.created_at or pending.updated_at
        return TransferAdmission(
            task_id=pending.task_id,
            storage=pending.storage,
            src_path=pending.src_path,
            state=pending.state,
            created_at=created_at,
            updated_at=pending.updated_at,
            last_error=pending.last_error,
        )

    def admit(self, *, storage: str, src_path: str) -> TransferAdmission:
        """幂等持久化准入事实，并返回跨重启稳定的任务标识。"""
        now_time = self._now()
        try:
            with self._session_factory() as session:
                transaction = SqlAlchemyUnitOfWork(session)
                try:
                    pending = TransferPendingOper(db=session).stage_admit(
                        task_id=uuid4().hex,
                        storage=storage,
                        src_path=src_path,
                        state=TRANSFER_ADMISSION_ACCEPTED,
                        now_time=now_time,
                    )
                    if pending is None:
                        raise ValueError("整理任务的存储与源路径不能为空")
                    session.flush()
                    admission = self._project(pending)
                    transaction.commit()
                    return admission
                except Exception:
                    transaction.rollback()
                    raise
        except IntegrityError as error:
            # 并发准入可能同时通过查询；唯一约束决定赢家，输家回读稳定身份。
            with self._session_factory() as session:
                pending = TransferPendingOper(db=session).get_by_identity(
                    storage=storage,
                    src_path=src_path,
                )
                if pending is None:
                    raise RuntimeError("并发准入冲突后未找到已提交记录") from error
                return self._project(pending)

    def list_accepted(self, limit: int = 5000) -> list[TransferAdmission]:
        """在独立只读会话中投影等待恢复或执行的准入记录。"""
        with self._session_factory() as session:
            pending_items = TransferPendingOper(db=session).list_by_state(
                state=TRANSFER_ADMISSION_ACCEPTED,
                limit=limit,
            )
            return [self._project(pending) for pending in pending_items]

    def record_enqueue_failure(self, *, task_id: str, error: str) -> None:
        """独立提交最近一次入队失败，保留准入记录供后续恢复。"""
        with self._session_factory() as session:
            transaction = SqlAlchemyUnitOfWork(session)
            try:
                TransferPendingOper(db=session).stage_record_enqueue_failure(
                    task_id=task_id,
                    error=error,
                    now_time=self._now(),
                )
                transaction.commit()
            except Exception:
                transaction.rollback()
                raise

    def discard_task(self, *, task_id: str) -> int:
        """在独立事务中按稳定任务标识删除已到终态的准入记录。"""
        with self._session_factory() as session:
            transaction = SqlAlchemyUnitOfWork(session)
            try:
                deleted = TransferPendingOper(db=session).stage_discard_task(
                    task_id=task_id,
                )
                transaction.commit()
                return deleted
            except Exception:
                transaction.rollback()
                raise
