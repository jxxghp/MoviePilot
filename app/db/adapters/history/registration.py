"""
外部历史登记的独立短 Session 与原子插入适配器。
"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict

from sqlalchemy.orm import Session

from app.application.history.contracts import TransferHistorySnapshot
from app.application.history.registration import (
    TransferRegistrationResult,
    TransferRegistrationScope,
    TransferRegistrationWrite,
)
from app.db.adapters.history.transfer import project_transfer_history
from app.db.oper.transferhistory import TransferHistoryOper
from app.db.uow import SqlAlchemyUnitOfWork


class SessionTransferRegistrationRepository:
    """
    将外部登记端口绑定到调用方短 Session，不持有提交权限。
    """

    def __init__(self, session: Session) -> None:
        """
        保存当前 Session 对应的表级访问对象。
        """
        self._oper = TransferHistoryOper(session)

    def get_by_source(self, src: str, storage: str) -> TransferHistorySnapshot | None:
        """
        在 Session 内完成现有源地址历史的冻结投影。
        """
        record = self._oper.get_by_src(src, storage)
        return project_transfer_history(record) if record is not None else None

    def insert_if_absent(self, write: TransferRegistrationWrite) -> TransferRegistrationResult:
        """
        将有界声明和服务器事实映射为列，再由唯一约束返回获胜历史。
        """
        values = asdict(write.request)
        values["media_source"] = write.request.media_source.value
        values.update(
            src_fileitem=write.source.to_dict(),
            dest_fileitem=write.destination.to_dict(),
            origin="external", registration=write.audit.model_dump(mode="json"),
            date=write.date, status=True, transfer_task_id=None,
            transfer_settlement_revision=None,
        )
        record, created = self._oper.stage_insert_if_source_absent(values)
        return TransferRegistrationResult(project_transfer_history(record), created)


class TransferRegistrationScopeFactory:
    """
    为每个查询或写入阶段单独创建短 Session，避免持有认证请求的旧读事务。
    """

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        """
        保存组合根提供的 Session 工厂。
        """
        self._session_factory = session_factory

    @contextmanager
    def __call__(self) -> Iterator[TransferRegistrationScope]:
        """
        保证 Session 关闭；提交和显式回滚由应用用例决定。
        """
        session = self._session_factory()
        try:
            yield TransferRegistrationScope(
                SessionTransferRegistrationRepository(session), SqlAlchemyUnitOfWork(session),
            )
        finally:
            session.close()
