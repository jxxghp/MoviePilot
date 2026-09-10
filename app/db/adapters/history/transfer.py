"""整理历史的类型化查询、写入与短事务适配器。"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from copy import deepcopy
from typing import Optional, TypeVar

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.application.history import (
    TransferHistoryMonthlyStatistics,
    TransferHistorySnapshot,
    TransferHistoryStatisticSnapshot,
    TransferHistoryWrite,
    failed_retry_count,
    max_failed_retries,
)
from app.application.transfer.feedback import cleanup_update_fields
from app.db.oper.transferhistory import TransferHistoryOper
from app.db.uow import SqlAlchemyAsyncUnitOfWork, SqlAlchemyUnitOfWork
from app.schemas.media import normalize_media_source
from app.schemas.types import MediaSource

ResultT = TypeVar("ResultT")


def project_transfer_history(record: object) -> TransferHistorySnapshot:
    """在 Session 内把整理历史 ORM 记录投影为不可变快照。"""
    history_id = getattr(record, "id", None)
    if not isinstance(history_id, int):
        raise ValueError("整理历史记录缺少稳定身份")
    media_source = normalize_media_source(getattr(record, "media_source", None))
    media_id_value = getattr(record, "media_id", None)
    media_id = str(media_id_value).strip() if media_id_value is not None else None
    if not media_source or not media_id or media_id == "0":
        media_source = None
        media_id = None
    source_payload = getattr(record, "src_fileitem", None)
    source_size = source_payload.get("size") if isinstance(source_payload, dict) else None
    source_modify_time = (
        source_payload.get("modify_time") if isinstance(source_payload, dict) else None
    )
    source_fileid = source_payload.get("fileid") if isinstance(source_payload, dict) else None
    persisted_retry_count = getattr(record, "retry_count", None)
    try:
        parsed_persisted_retry_count = (
            max(int(persisted_retry_count), 0)
            if persisted_retry_count is not None
            else None
        )
    except (TypeError, ValueError):
        parsed_persisted_retry_count = None
    status = bool(getattr(record, "status", False))
    retry_count = (
        parsed_persisted_retry_count
        if parsed_persisted_retry_count is not None
        else (
            failed_retry_count(
                getattr(record, "src", None),
                getattr(record, "src_storage", None),
                file_size=source_size,
                file_modify_time=source_modify_time,
                fileid=source_fileid,
            )
            if not status
            else 0
        )
    )
    retry_exhausted = bool(getattr(record, "auto_paused", False)) or (
        retry_count >= max_failed_retries() if retry_count else False
    )
    failure_stage = getattr(record, "failure_stage", None)
    recovery_action = getattr(record, "recovery_action", None)
    cleanup_status = getattr(record, "cleanup_status", None)
    if cleanup_status == "failed":
        # 入库成功但下载器清理失败是独立终态，不能被 errmsg 为空时的默认
        # transfer 阶段覆盖，否则历史页会给出错误的排障方向。
        from app.application.transfer.feedback import classify_transfer_failure

        cleanup_feedback = classify_transfer_failure("下载器清理失败")
        failure_stage = failure_stage or cleanup_feedback.stage.value
        recovery_action = recovery_action or cleanup_feedback.action
    elif not status and (not failure_stage or not recovery_action):
        from app.application.transfer.feedback import classify_transfer_failure

        feedback = classify_transfer_failure(getattr(record, "errmsg", None))
        failure_stage = failure_stage or feedback.stage.value
        recovery_action = recovery_action or feedback.action
    elif status:
        # 成功记录的失败字段不能被历史投影中的默认反馈重新带出，避免误导前端。
        failure_stage = None
        recovery_action = None
    return TransferHistorySnapshot(
        id=history_id,
        transfer_task_id=getattr(record, "transfer_task_id", None),
        transfer_settlement_revision=getattr(
            record,
            "transfer_settlement_revision",
            None,
        ),
        src=getattr(record, "src", None),
        src_storage=getattr(record, "src_storage", None),
        src_fileitem=deepcopy(getattr(record, "src_fileitem", None)),
        dest=getattr(record, "dest", None),
        dest_storage=getattr(record, "dest_storage", None),
        dest_fileitem=deepcopy(getattr(record, "dest_fileitem", None)),
        mode=getattr(record, "mode", None),
        type=getattr(record, "type", None),
        media_category_id=getattr(record, "media_category_id", None),
        category=getattr(record, "category", None),
        classification_rule_id=getattr(record, "classification_rule_id", None),
        classification_policy_revision=getattr(
            record,
            "classification_policy_revision",
            None,
        ),
        classification_source=getattr(record, "classification_source", None),
        title=getattr(record, "title", None),
        year=getattr(record, "year", None),
        media_source=media_source,
        media_id=media_id,
        music_type=getattr(record, "music_type", None),
        total_tracks=getattr(record, "total_tracks", None),
        audio_format=getattr(record, "audio_format", None),
        audio_lossless=getattr(record, "audio_lossless", None),
        bit_depth=getattr(record, "bit_depth", None),
        sample_rate=getattr(record, "sample_rate", None),
        bitrate=getattr(record, "bitrate", None),
        seasons=getattr(record, "seasons", None),
        episodes=getattr(record, "episodes", None),
        image=getattr(record, "image", None),
        downloader=getattr(record, "downloader", None),
        download_hash=getattr(record, "download_hash", None),
        status=status,
        errmsg=getattr(record, "errmsg", None),
        failure_stage=failure_stage,
        recovery_action=recovery_action,
        retry_count=retry_count,
        retry_exhausted=retry_exhausted,
        auto_paused=bool(getattr(record, "auto_paused", False)) or retry_exhausted,
        cleanup_status=cleanup_status,
        cleanup_error=getattr(record, "cleanup_error", None),
        date=getattr(record, "date", None),
        files=deepcopy(getattr(record, "files", None)),
        episode_group=getattr(record, "episode_group", None),
    )


class TransactionalTransferHistoryRepository:
    """为整理、Agent、工作流和历史接口创建短生命周期 Session。"""

    def __init__(
        self,
        *,
        sync_session: Callable[[], Session],
        async_session: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    ) -> None:
        """保存由启动组合根提供的同步与异步 Session 工厂。"""
        self._sync_session = sync_session
        self._async_session = async_session

    def _read(self, operation: Callable[[TransferHistoryOper], ResultT]) -> ResultT:
        """在独立同步 Session 中执行一次只读操作。"""
        session = self._sync_session()
        try:
            return operation(TransferHistoryOper(session))
        finally:
            session.close()

    def _write(self, operation: Callable[[TransferHistoryOper], ResultT]) -> ResultT:
        """在独立同步 Session 和单一 UoW 中执行一次写操作。"""
        session = self._sync_session()
        unit_of_work = SqlAlchemyUnitOfWork(session)
        try:
            result = operation(TransferHistoryOper(session))
            unit_of_work.commit()
            return result
        except Exception:
            unit_of_work.rollback()
            raise
        finally:
            session.close()

    def get(self, history_id: int) -> Optional[TransferHistorySnapshot]:
        """按主键返回整理历史快照。"""
        return self._read(
            lambda repository: (
                project_transfer_history(record)
                if (record := repository.get(history_id)) is not None
                else None
            )
        )

    def get_by_src(
        self,
        src: str,
        storage: Optional[str] = None,
    ) -> Optional[TransferHistorySnapshot]:
        """按源路径和可选存储返回最新历史快照。"""
        return self._read(
            lambda repository: (
                project_transfer_history(record)
                if (record := repository.get_by_src(src, storage)) is not None
                else None
            )
        )

    def get_success_by_src(
        self,
        src: str,
        storage: Optional[str] = None,
    ) -> Optional[TransferHistorySnapshot]:
        """按源路径和可选存储返回最新成功历史快照。"""
        return self._read(
            lambda repository: (
                project_transfer_history(record)
                if (record := repository.get_success_by_src(src, storage)) is not None
                else None
            )
        )

    def get_by_dest(
        self,
        dest: str,
        storage: Optional[str] = None,
    ) -> Optional[TransferHistorySnapshot]:
        """按目标路径和可选存储返回最新历史快照。"""
        return self._read(
            lambda repository: (
                project_transfer_history(record)
                if (record := repository.get_by_dest(dest, storage)) is not None
                else None
            )
        )

    def get_by_transfer_task_id(
        self,
        *,
        task_id: str,
    ) -> Optional[TransferHistorySnapshot]:
        """按 durable 整理任务标识返回终态历史快照。"""
        return self._read(
            lambda repository: (
                project_transfer_history(record)
                if (
                    record := repository.get_by_transfer_task_id(task_id=task_id)
                ) is not None
                else None
            )
        )

    async def async_get_by_transfer_task_id(
        self,
        *,
        task_id: str,
    ) -> Optional[TransferHistorySnapshot]:
        """异步按 durable 整理任务标识返回终态历史快照。"""
        async with self._async_session() as session:
            record = await TransferHistoryOper(session).async_get_by_transfer_task_id(
                task_id=task_id
            )
            return project_transfer_history(record) if record is not None else None

    def get_by_media_identity(
        self,
        media_source: MediaSource,
        media_id: str,
        mtype: Optional[str] = None,
    ) -> Optional[TransferHistorySnapshot]:
        """按规范媒体身份和可选媒体类型返回历史快照。"""
        return self._read(
            lambda repository: (
                project_transfer_history(record)
                if (
                    record := repository.get_by_media_identity(
                        media_source,
                        media_id,
                        mtype,
                    )
                ) is not None
                else None
            )
        )

    def list_success_by_src(
        self,
        src: str,
        storage: Optional[str] = None,
        recursive: bool = False,
    ) -> list[TransferHistorySnapshot]:
        """按源路径返回成功整理历史快照。"""
        return self._read(
            lambda repository: [
                project_transfer_history(record)
                for record in repository.list_success_by_src(
                    src,
                    storage,
                    recursive,
                )
            ]
        )

    def list_success_move_by_dest(
        self,
        dest: str,
        storage: Optional[str] = None,
        recursive: bool = False,
    ) -> list[TransferHistorySnapshot]:
        """按目标路径返回成功移动历史快照。"""
        return self._read(
            lambda repository: [
                project_transfer_history(record)
                for record in repository.list_success_move_by_dest(
                    dest,
                    storage,
                    recursive,
                )
            ]
        )

    def list_by_hash(self, download_hash: str) -> list[TransferHistorySnapshot]:
        """按下载任务 Hash 返回历史快照。"""
        return self._read(
            lambda repository: [
                project_transfer_history(record)
                for record in repository.list_by_hash(download_hash)
            ]
        )

    async def async_get(
        self,
        history_id: int,
    ) -> Optional[TransferHistorySnapshot]:
        """异步按主键返回整理历史快照。"""
        async with self._async_session() as session:
            record = await TransferHistoryOper(session).async_get(history_id)
            return project_transfer_history(record) if record is not None else None

    async def async_list_by_title(
        self,
        title: str,
        page: int = 1,
        count: int = 30,
        status: Optional[bool] = None,
        wildcard: bool = False,
    ) -> list[TransferHistorySnapshot]:
        """异步按标题或路径分页返回历史快照。"""
        async with self._async_session() as session:
            records = await TransferHistoryOper(session).async_list_by_title(
                title,
                page,
                count,
                status,
                wildcard,
            )
            return [project_transfer_history(record) for record in records]

    async def async_list_by_page(
        self,
        page: int = 1,
        count: int = 30,
        status: Optional[bool] = None,
    ) -> list[TransferHistorySnapshot]:
        """异步按时间倒序分页返回历史快照。"""
        async with self._async_session() as session:
            records = await TransferHistoryOper(session).async_list_by_page(
                page,
                count,
                status,
            )
            return [project_transfer_history(record) for record in records]

    async def async_count(self, status: Optional[bool] = None) -> int:
        """异步统计指定状态的整理历史数量。"""
        async with self._async_session() as session:
            count = await TransferHistoryOper(session).async_count(status)
            return int(count or 0)

    async def async_count_by_title(
        self,
        title: str,
        status: Optional[bool] = None,
        wildcard: bool = False,
    ) -> int:
        """异步统计匹配标题或路径的整理历史数量。"""
        async with self._async_session() as session:
            count = await TransferHistoryOper(session).async_count_by_title(
                title,
                status,
                wildcard,
            )
            return int(count or 0)

    async def async_statistic(
        self,
        days: int = 7,
    ) -> list[TransferHistoryStatisticSnapshot]:
        """异步返回最近若干天的每日整理数量。"""
        async with self._async_session() as session:
            rows = await TransferHistoryOper(session).async_statistic(days)
            return [
                TransferHistoryStatisticSnapshot(
                    date=str(row[0]),
                    count=int(row[1]),
                )
                for row in rows
            ]

    def monthly_media_statistics(self) -> TransferHistoryMonthlyStatistics:
        """返回本月电影、剧集、单集和音乐整理数量。"""
        movies, tv_shows, episodes, music = self._read(
            lambda repository: repository.monthly_media_statistics()
        )
        return TransferHistoryMonthlyStatistics(
            movies=movies,
            tv_shows=tv_shows,
            episodes=episodes,
            music=music,
        )

    def replace(self, history: TransferHistoryWrite) -> TransferHistorySnapshot:
        """在独立事务中替换同源历史并返回快照。"""
        return self._write(
            lambda repository: project_transfer_history(
                repository.stage_replace_by_src(**history.to_payload())
            )
        )

    def delete(self, history_id: int) -> None:
        """在独立事务中删除一条没有 durable 任务映射的旧历史。"""
        self._write(lambda repository: repository.stage_delete(history_id))

    async def async_delete(self, history_id: int) -> None:
        """在独立异步事务中删除一条没有 durable 任务映射的旧历史。"""
        async with self._async_session() as session:
            unit_of_work = SqlAlchemyAsyncUnitOfWork(session)
            try:
                await TransferHistoryOper(session).async_stage_delete(history_id)
                await unit_of_work.commit()
            except Exception:
                await unit_of_work.rollback()
                raise

    def truncate(self) -> None:
        """在独立事务中清空没有 durable 任务映射的历史。"""
        self._write(lambda repository: repository.stage_truncate())

    def update_download_hash(self, history_id: int, download_hash: str) -> None:
        """在独立事务中补充整理历史的下载任务 Hash。"""
        self._write(
            lambda repository: repository.stage_update_download_hash(
                history_id,
                download_hash,
            )
        )

    def update_cleanup_status(
            self,
            history_id: int,
            status: str,
            error: Optional[str] = None,
    ) -> None:
        """在独立事务中记录媒体入库后的下载器清理结果。"""
        self._write(
            lambda repository: repository.stage_update_cleanup_status(
                history_id,
                cleanup_update_fields(status, error),
            )
        )


class SessionTransferHistoryRepository:
    """把 API 请求持有的同步 Session 适配为整理历史查询和暂存端口。"""

    def __init__(self, session: Session) -> None:
        """保存由请求依赖独占的同步数据库 Session。"""
        self._session = session

    def get(self, history_id: int) -> Optional[TransferHistorySnapshot]:
        """在请求 Session 内读取并立即投影整理历史。"""
        record = TransferHistoryOper(self._session).get(history_id)
        return project_transfer_history(record) if record is not None else None

    def replace(self, history: TransferHistoryWrite) -> TransferHistorySnapshot:
        """在请求 Session 内暂存同源替换并返回冻结快照。"""
        record = TransferHistoryOper(self._session).stage_replace_by_src(
            **history.to_payload()
        )
        return project_transfer_history(record)

    def stage_delete(self, history_id: int) -> None:
        """在请求 Session 内暂存一条旧整理历史删除。"""
        TransferHistoryOper(self._session).stage_delete(history_id)

    def stage_truncate(self) -> None:
        """在请求 Session 内暂存全部旧整理历史删除。"""
        TransferHistoryOper(self._session).stage_truncate()

    def stage_update_cleanup_status(
        self,
        history_id: int,
        cleanup_status: str,
        cleanup_error: Optional[str] = None,
    ) -> None:
        """在请求 Session 内暂存下载器清理状态更新。"""
        TransferHistoryOper(self._session).stage_update_cleanup_status(
            history_id,
            cleanup_update_fields(cleanup_status, cleanup_error),
        )
