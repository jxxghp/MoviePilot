"""Chain durable 事件写入端口的 SQLAlchemy 启动适配器。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sqlalchemy.orm import Session

from app.application.orchestration.durable_events import (
    ChainDurableEventWriter,
    TransferHistoryRef,
    download_added_event_key,
    snapshot_download_added,
    snapshot_transfer_result,
    transfer_result_event_key,
)
from app.application.history import TransferHistoryRecord, TransferHistoryWriter
from app.application.outbox import DurableEventCommand, OutboxIntent
from app.db.oper.downloadhistory import DownloadHistoryOper
from app.db.oper.transferhistory import TransferHistoryOper
from app.db.uow import SqlAlchemyUnitOfWork
from app.startup.ports.outbox import SqlAlchemyOutboxRepository


class _StagingTransferHistoryWriter:
    """让既有历史字段映射复用无提交的 replace 适配器。"""

    def __init__(self, repository: TransferHistoryOper) -> None:
        """保存绑定调用方 Session 的整理历史仓储。"""
        self._repository = repository

    def get_by_src(
        self,
        src: str,
        storage: str | None = None,
    ) -> TransferHistoryRecord | None:
        """转发按源路径读取。"""
        return self._repository.get_by_src(src, storage)

    def get_success_by_src(
        self,
        src: str,
        storage: str | None = None,
    ) -> TransferHistoryRecord | None:
        """转发按源路径读取成功记录。"""
        return self._repository.get_success_by_src(src, storage)

    def add_force(self, **payload: Any) -> TransferHistoryRecord:
        """保持应用层旧端口名，但只暂存替换而不自行提交。"""
        return self._repository.stage_replace_by_src(**payload)


class TransactionalChainDurableEventWriter(ChainDurableEventWriter):
    """为每次 Chain 结果事件创建独占同步 Session 和 UoW。"""

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        """注入惰性同步 Session 工厂。"""
        self._session_factory = session_factory

    def download_added(
        self,
        *,
        history_payload: dict[str, Any],
        file_payloads: list[dict[str, Any]],
        event_payload: dict[str, Any],
        after_commit: Callable[[], None],
        publish: Callable[[dict[str, Any]], None],
    ) -> None:
        """原子写下载历史、文件清单和 DownloadAdded intent。"""
        session = self._session_factory()
        try:
            repository = DownloadHistoryOper(session)
            outbox = SqlAlchemyOutboxRepository(session)
            command = DurableEventCommand(
                unit_of_work=SqlAlchemyUnitOfWork(session),
                outbox=outbox,
            )
            event_key = download_added_event_key(event_payload)
            event_payload["idempotency_key"] = event_key

            def stage_business() -> None:
                """在同一事务暂存下载历史和可选文件清单。"""
                repository.stage_add(history_payload)
                if file_payloads:
                    repository.stage_add_files(file_payloads)

            command.execute(
                intent=OutboxIntent(
                    event_key=event_key,
                    topic="download.added",
                    payload=snapshot_download_added(event_payload),
                ),
                stage_business=stage_business,
                after_commit=after_commit,
                publish=lambda: publish(event_payload),
            )
        finally:
            session.close()

    def transfer_result(
        self,
        *,
        topic: str,
        stage_history: Callable[[TransferHistoryWriter], TransferHistoryRecord | None],
        event_payload: dict[str, Any],
        publish: Callable[[dict[str, Any]], None],
    ) -> TransferHistoryRecord | None:
        """原子写整理历史与结果 intent，并返回脱离 Session 的最小投影。"""
        session = self._session_factory()
        try:
            staging = _StagingTransferHistoryWriter(TransferHistoryOper(session))
            command = DurableEventCommand(
                unit_of_work=SqlAlchemyUnitOfWork(session),
                outbox=SqlAlchemyOutboxRepository(session),
            )

            def stage_business() -> TransferHistoryRef | None:
                """复用历史字段映射，并在 flush 后冻结安全投影。"""
                history = stage_history(staging)
                if history is None:
                    return None
                return TransferHistoryRef(
                    id=history.id,
                    status=bool(history.status),
                    src=history.src,
                    src_storage=history.src_storage,
                    src_fileitem=history.src_fileitem,
                )

            def build_intent(
                history: TransferHistoryRef | None,
            ) -> OutboxIntent:
                """历史 ID 确定后构造事件键与可恢复快照。"""
                if history is None:
                    raise RuntimeError("整理历史暂存失败，无法登记 durable 结果事件")
                event_key = transfer_result_event_key(topic, history.id)
                event_payload["transfer_history_id"] = history.id
                event_payload["idempotency_key"] = event_key
                return OutboxIntent(
                    event_key=event_key,
                    topic=topic,
                    payload=snapshot_transfer_result(event_payload),
                )

            return command.execute(
                intent=build_intent,
                stage_business=stage_business,
                publish=lambda: publish(event_payload),
            )
        finally:
            session.close()
