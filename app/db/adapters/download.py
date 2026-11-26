"""
下载失败冷却与待确认下载切片的显式会话与事务适配器。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from types import MappingProxyType
from typing import Any, Optional, TypeVar

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.application.download.failures import (
    DownloadFailureSnapshot,
    DownloadFailureWrite,
)
from app.application.download.pending import (
    DownloadPendingSnapshot,
    DownloadPendingWrite,
)
from app.db.models.downloadpending import DownloadPending
from app.db.oper.downloadfailure import DownloadFailureOper
from app.db.oper.downloadpending import DownloadPendingOper
from app.db.uow import SqlAlchemyAsyncUnitOfWork, SqlAlchemyUnitOfWork
from app.schemas.media import normalize_media_source

_T = TypeVar("_T")


def _freeze_json_value(value: Any) -> Any:
    """
    将脱离数据库会话的 JSON 字段递归转换为不可变结构，避免调用方修改共享快照。
    """
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze_json_value(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_json_value(item) for item in value)
    return value


def _thaw_json_value(value: Any) -> Any:
    """
    将不可变快照载荷还原为标准 JSON 容器，便于 SQLAlchemy 序列化写库。
    """
    if isinstance(value, Mapping):
        return {str(key): _thaw_json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_thaw_json_value(item) for item in value]
    return value


def _project_pending_snapshot(record: DownloadPending) -> DownloadPendingSnapshot:
    """
    在数据库会话关闭前将待确认 ORM 记录投影为只读快照。
    """
    raw_episodes = record.episodes
    episodes = (
        tuple(int(item) for item in raw_episodes if isinstance(item, int))
        if isinstance(raw_episodes, list)
        else None
    )
    raw_hashes = record.hashes
    hashes = (
        tuple(str(item).lower() for item in raw_hashes if item)
        if isinstance(raw_hashes, list)
        else ()
    )
    payload = _freeze_json_value(record.payload) if isinstance(record.payload, Mapping) else None
    return DownloadPendingSnapshot(
        pending_key=record.pending_key,
        target_key=record.target_key,
        submitted_at=record.submitted_at,
        expires_at=record.expires_at,
        media_type=record.type,
        title=record.title,
        year=record.year,
        media_source=normalize_media_source(record.media_source),
        media_id=record.media_id,
        season=record.season,
        episodes=episodes,
        downloader=record.downloader,
        hashes=hashes,
        torrent_name=record.torrent_name,
        source=record.source,
        subscription_id=record.subscription_id,
        payload=payload,
        last_checked_at=record.last_checked_at,
    )


class TransactionalDownloadFailureRepository:
    """
    为 Chain 下载失败读写创建短生命周期会话并显式收口事务。
    """

    def __init__(self, session_factory: Callable[[], Session],
                 async_session_factory: Optional[Callable[[], AbstractAsyncContextManager[AsyncSession]]] = None) -> None:
        """
        保存由启动组合根提供的同步与可选异步短会话工厂。

        :param session_factory: 同步择源使用的独立数据库会话工厂
        :param async_session_factory: 人工重试清理使用的异步会话工厂；未装配时拒绝异步删除
        """
        self._session_factory = session_factory
        self._async_session_factory = async_session_factory

    def get_active_by_fingerprints(
        self,
        fingerprints: list[str],
        now_time: str,
    ) -> dict[str, DownloadFailureSnapshot]:
        """
        在独立只读会话中查询仍处于冷却期的失败记录。
        """
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
        """
        在一个显式 UoW 中新增或更新下载失败记录。
        """
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


    async def async_delete_by_fingerprints(self, fingerprints: list[str]) -> int:
        """
        以短事务清除人工确认资源的失败冷却，提交失败时保留原记录并向调用方报告。

        :param fingerprints: 需要清除的普通资源失败指纹
        :return: 已成功提交删除的记录数
        """
        if self._async_session_factory is None:
            raise RuntimeError("下载失败清理异步会话尚未装配")
        async with self._async_session_factory() as session:
            transaction = SqlAlchemyAsyncUnitOfWork(session)
            try:
                count: int = await session.run_sync(
                    lambda sync_session: DownloadFailureOper(db=sync_session).delete_by_fingerprints(fingerprints)
                )
                await transaction.commit()
                return count
            except Exception:
                await transaction.rollback()
                raise


class TransactionalDownloadPendingRepository:
    """
    为 Chain 待确认下载状态读写创建短生命周期会话并显式收口事务。
    """

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        """
        保存由启动组合根提供的同步会话工厂。
        """
        self._session_factory = session_factory

    def list_all(self) -> list[DownloadPendingSnapshot]:
        """
        在独立只读会话中查询全部待确认下载记录。
        """
        with self._session_factory() as session:
            records = DownloadPendingOper(db=session).list_all()
            return [_project_pending_snapshot(record) for record in records]

    def list_by_target_keys(self, target_keys: list[str]) -> list[DownloadPendingSnapshot]:
        """
        在独立只读会话中按媒体目标键批量查询待确认记录。
        """
        with self._session_factory() as session:
            records = DownloadPendingOper(db=session).list_by_target_keys(target_keys)
            return [_project_pending_snapshot(record) for record in records]

    def list_by_subscription_id(self, subscription_id: int) -> list[DownloadPendingSnapshot]:
        """
        在独立只读会话中按订阅主键查询待确认记录。
        """
        with self._session_factory() as session:
            records = DownloadPendingOper(db=session).list_by_subscription_id(subscription_id)
            return [_project_pending_snapshot(record) for record in records]

    def get_by_key(self, pending_key: str) -> Optional[DownloadPendingSnapshot]:
        """
        在独立只读会话中按唯一键读取单条待确认记录。
        """
        with self._session_factory() as session:
            record = DownloadPendingOper(db=session).get_by_key(pending_key)
            return _project_pending_snapshot(record) if record is not None else None

    def _execute_write(self, operation: Callable[[Session], _T]) -> _T:
        """
        在短生命周期显式 UoW 中执行写操作，异常时回滚并向上传播。
        """
        with self._session_factory() as session:
            transaction = SqlAlchemyUnitOfWork(session)
            try:
                result = operation(session)
                transaction.commit()
                return result
            except Exception:
                transaction.rollback()
                raise

    def upsert(self, record: DownloadPendingWrite) -> None:
        """
        在显式 UoW 中新增或更新一条待确认下载记录。
        """
        self._execute_write(
            lambda session: DownloadPendingOper(db=session).upsert(
                pending_key=record.pending_key,
                target_key=record.target_key,
                type=record.media_type,
                title=record.title,
                year=record.year,
                media_source=record.media_source,
                media_id=record.media_id,
                season=record.season,
                episodes=list(record.episodes) if record.episodes is not None else None,
                downloader=record.downloader,
                hashes=list(record.hashes),
                torrent_name=record.torrent_name,
                source=record.source,
                subscription_id=record.subscription_id,
                payload=_thaw_json_value(record.payload) if record.payload is not None else None,
                submitted_at=record.submitted_at,
                last_checked_at=record.last_checked_at,
                expires_at=record.expires_at,
            )
        )

    def touch_checked(self, pending_key: str, checked_at: str) -> None:
        """
        在显式 UoW 中更新待确认记录的最近核对时间。
        """
        self._execute_write(
            lambda session: DownloadPendingOper(db=session).touch_checked(
                pending_key=pending_key,
                checked_at=checked_at,
            )
        )

    def delete_by_key(self, pending_key: str) -> int:
        """
        在显式 UoW 中按唯一键删除待确认记录。
        """
        return self._execute_write(
            lambda session: DownloadPendingOper(db=session).delete_by_key(pending_key)
        )

    def delete_by_target_key(self, target_key: str) -> int:
        """
        在显式 UoW 中按媒体目标键删除关联的待确认记录。
        """
        return self._execute_write(
            lambda session: DownloadPendingOper(db=session).delete_by_target_key(target_key)
        )
