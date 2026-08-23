"""下载失败冷却切片的显式会话与事务适配器。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

from app.db.oper.downloadfailure import DownloadFailureOper
from app.db.uow import SqlAlchemyUnitOfWork


class TransactionalDownloadFailureRepository:
    """为 Chain 下载失败读写创建短生命周期会话并显式收口事务。"""

    def __init__(self, session_factory: Callable[[], Any]) -> None:
        """保存由启动组合根提供的同步会话工厂。"""
        self._session_factory = session_factory

    def get_active_by_fingerprints(
        self,
        fingerprints: list[str],
        now_time: str,
    ) -> dict[str, Any]:
        """在独立只读会话中查询仍处于冷却期的失败记录。"""
        with self._session_factory() as session:
            return cast(
                dict[str, Any],
                DownloadFailureOper(db=session).get_active_by_fingerprints(
                    fingerprints=fingerprints,
                    now_time=now_time,
                ),
            )

    def record_failure(
        self,
        fingerprint: str,
        now_time: str,
        next_retry_at: str,
        **kwargs: object,
    ) -> Any:
        """在一个显式 UoW 中新增或更新下载失败记录。"""
        with self._session_factory() as session:
            transaction = SqlAlchemyUnitOfWork(session)
            try:
                failure = DownloadFailureOper(db=session).record_failure(
                    fingerprint=fingerprint,
                    now_time=now_time,
                    next_retry_at=next_retry_at,
                    **kwargs,
                )
                transaction.commit()
                return failure
            except Exception:
                transaction.rollback()
                raise
