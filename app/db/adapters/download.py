"""下载失败冷却切片的显式会话与事务适配器。"""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy.orm import Session

from app.application.download.failures import (
    DownloadFailureSnapshot,
    DownloadFailureWrite,
)
from app.db.oper.downloadfailure import DownloadFailureOper
from app.db.uow import SqlAlchemyUnitOfWork


class TransactionalDownloadFailureRepository:
    """为 Chain 下载失败读写创建短生命周期会话并显式收口事务。"""

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        """保存由启动组合根提供的同步会话工厂。"""
        self._session_factory = session_factory

    def get_active_by_fingerprints(
        self,
        fingerprints: list[str],
        now_time: str,
    ) -> dict[str, DownloadFailureSnapshot]:
        """在独立只读会话中查询仍处于冷却期的失败记录。"""
        with self._session_factory() as session:
            records = DownloadFailureOper(db=session).get_active_by_fingerprints(
                fingerprints=fingerprints,
                now_time=now_time,
            )
            return {
                fingerprint: DownloadFailureSnapshot(
                    fingerprint=record.fingerprint,
                    error_message=record.error_message,
                    next_retry_at=record.next_retry_at,
                )
                for fingerprint, record in records.items()
            }

    def record_failure(self, failure: DownloadFailureWrite) -> None:
        """在一个显式 UoW 中新增或更新下载失败记录。"""
        with self._session_factory() as session:
            transaction = SqlAlchemyUnitOfWork(session)
            try:
                DownloadFailureOper(db=session).record_failure(
                    fingerprint=failure.fingerprint,
                    now_time=failure.failed_at,
                    next_retry_at=failure.next_retry_at,
                    type=failure.media_type,
                    title=failure.title,
                    year=failure.year,
                    media_source=failure.media_source,
                    media_id=failure.media_id,
                    seasons=failure.seasons,
                    episodes=failure.episodes,
                    site=failure.site,
                    site_name=failure.site_name,
                    torrent_id=failure.torrent_id,
                    torrent_name=failure.torrent_name,
                    torrent_size=failure.torrent_size,
                    downloader=failure.downloader,
                    source=failure.source,
                    error_message=failure.error_message,
                )
                transaction.commit()
            except Exception:
                transaction.rollback()
                raise
