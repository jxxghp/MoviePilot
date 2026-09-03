"""下载历史的类型化查询、写入与事务适配器。"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from copy import deepcopy
from typing import Optional, TypeVar, Union

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.application.history import (
    DownloadFileSnapshot,
    DownloadFileWrite,
    DownloadHistorySnapshot,
    DownloadHistoryWrite,
)
from app.db.oper.downloadhistory import DownloadHistoryOper
from app.db.uow import SqlAlchemyAsyncUnitOfWork, SqlAlchemyUnitOfWork
from app.schemas.media import normalize_media_source
from app.schemas.types import MediaSource

ResultT = TypeVar("ResultT")


def _project_history(record: object) -> DownloadHistorySnapshot:
    """在 Session 内把下载历史 ORM 记录投影为不可变快照。"""
    history_id = getattr(record, "id", None)
    path = getattr(record, "path", None)
    media_type = getattr(record, "type", None)
    title = getattr(record, "title", None)
    if (
        not isinstance(history_id, int)
        or not isinstance(path, str)
        or not isinstance(media_type, str)
        or not isinstance(title, str)
    ):
        raise ValueError("下载历史记录缺少稳定身份、路径、类型或标题")
    media_source = normalize_media_source(getattr(record, "media_source", None))
    media_id_value = getattr(record, "media_id", None)
    media_id = str(media_id_value).strip() if media_id_value is not None else None
    if not media_source or not media_id or media_id == "0":
        media_source = None
        media_id = None
    return DownloadHistorySnapshot(
        id=history_id,
        path=path,
        type=media_type,
        title=title,
        year=getattr(record, "year", None),
        media_source=media_source,
        media_id=media_id,
        music_type=getattr(record, "music_type", None),
        seasons=getattr(record, "seasons", None),
        episodes=getattr(record, "episodes", None),
        image=getattr(record, "image", None),
        poster=getattr(record, "poster", None),
        downloader=getattr(record, "downloader", None),
        download_hash=getattr(record, "download_hash", None),
        torrent_name=getattr(record, "torrent_name", None),
        torrent_description=getattr(record, "torrent_description", None),
        torrent_site=getattr(record, "torrent_site", None),
        userid=(
            str(userid_value)
            if (userid_value := getattr(record, "userid", None)) is not None
            else None
        ),
        username=getattr(record, "username", None),
        channel=getattr(record, "channel", None),
        date=getattr(record, "date", None),
        note=deepcopy(getattr(record, "note", None)),
        media_category_id=getattr(record, "media_category_id", None),
        media_category=getattr(record, "media_category", None),
        classification_rule_id=getattr(record, "classification_rule_id", None),
        classification_policy_revision=getattr(
            record,
            "classification_policy_revision",
            None,
        ),
        classification_source=getattr(record, "classification_source", None),
        episode_group=getattr(record, "episode_group", None),
        custom_words=getattr(record, "custom_words", None),
    )


def _project_file(record: object) -> DownloadFileSnapshot:
    """在 Session 内把下载文件 ORM 记录投影为不可变快照。"""
    file_id = getattr(record, "id", None)
    state = getattr(record, "state", None)
    if not isinstance(file_id, int) or not isinstance(state, int):
        raise ValueError("下载文件记录缺少稳定身份或状态")
    return DownloadFileSnapshot(
        id=file_id,
        downloader=getattr(record, "downloader", None),
        download_hash=getattr(record, "download_hash", None),
        fullpath=getattr(record, "fullpath", None),
        savepath=getattr(record, "savepath", None),
        filepath=getattr(record, "filepath", None),
        torrentname=getattr(record, "torrentname", None),
        state=state,
    )


class TransactionalDownloadHistoryRepository:
    """为 Chain 和 Agent 下载历史读写创建短生命周期 Session。"""

    def __init__(
        self,
        *,
        sync_session: Callable[[], Session],
        async_session: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    ) -> None:
        """保存由启动组合根提供的同步与异步 Session 工厂。"""
        self._sync_session = sync_session
        self._async_session = async_session

    def _read(self, operation: Callable[[DownloadHistoryOper], ResultT]) -> ResultT:
        """在独立同步 Session 中执行一次只读操作。"""
        session = self._sync_session()
        try:
            return operation(DownloadHistoryOper(session))
        finally:
            session.close()

    def get_by_hash(
        self,
        download_hash: str,
    ) -> Optional[DownloadHistorySnapshot]:
        """按下载任务 Hash 返回最新历史快照。"""
        return self._read(
            lambda repository: (
                _project_history(record)
                if (record := repository.get_by_hash(download_hash)) is not None
                else None
            )
        )

    def get_by_hashes(
        self,
        download_hashes: list[str],
    ) -> dict[str, DownloadHistorySnapshot]:
        """批量返回以下载任务 Hash 为键的最新历史快照。"""
        return self._read(
            lambda repository: {
                download_hash: _project_history(record)
                for download_hash, record in repository.get_by_hashes(download_hashes).items()
            }
        )

    def get_by_path(self, path: str) -> Optional[DownloadHistorySnapshot]:
        """按下载保存路径返回历史快照。"""
        return self._read(
            lambda repository: (
                _project_history(record)
                if (record := repository.get_by_path(path)) is not None
                else None
            )
        )

    def get_by_media_identity(
        self,
        media_source: MediaSource,
        media_id: str,
        music_type: Optional[str] = None,
    ) -> list[DownloadHistorySnapshot]:
        """按规范媒体身份返回历史快照。"""
        return self._read(
            lambda repository: [
                _project_history(record)
                for record in repository.get_by_media_identity(
                    media_source=media_source,
                    media_id=media_id,
                    music_type=music_type,
                )
            ]
        )

    def get_file_by_fullpath(
        self,
        fullpath: str,
    ) -> Optional[DownloadFileSnapshot]:
        """按完整路径返回一条有效下载文件快照。"""
        return self._read(
            lambda repository: (
                _project_file(record)
                if (record := repository.get_file_by_fullpath(fullpath)) is not None
                else None
            )
        )

    def get_files_by_hash(
        self,
        download_hash: str,
        state: Optional[int] = None,
    ) -> list[DownloadFileSnapshot]:
        """按下载任务 Hash 返回文件快照。"""
        return self._read(
            lambda repository: [
                _project_file(record)
                for record in repository.get_files_by_hash(
                    download_hash,
                    state=state,
                )
            ]
        )

    def get_files_by_savepath(self, savepath: str) -> list[DownloadFileSnapshot]:
        """按保存目录返回下载文件快照。"""
        return self._read(
            lambda repository: [
                _project_file(record)
                for record in repository.get_files_by_savepath(savepath)
            ]
        )

    async def async_list_by_page(
        self,
        page: int = 1,
        count: int = 30,
    ) -> list[DownloadHistorySnapshot]:
        """在独立异步 Session 内分页读取并投影历史。"""
        async with self._async_session() as session:
            records = await DownloadHistoryOper(session).async_list_by_page(
                page,
                count,
            )
            return [_project_history(record) for record in records]

    async def async_count(self) -> int:
        """在独立异步 Session 内统计下载历史总数。"""
        async with self._async_session() as session:
            return await DownloadHistoryOper(session).async_count()

    def add(
        self,
        history: DownloadHistoryWrite,
        files: tuple[DownloadFileWrite, ...] = (),
    ) -> int:
        """在一个同步事务中新增历史与关联文件。"""
        session = self._sync_session()
        unit_of_work = SqlAlchemyUnitOfWork(session)
        try:
            repository = DownloadHistoryOper(session)
            record = repository.stage_add(history.to_payload())
            if files:
                repository.stage_add_files([file_item.to_payload() for file_item in files])
            history_id = int(record.id)
            unit_of_work.commit()
            return history_id
        except Exception:
            unit_of_work.rollback()
            raise
        finally:
            session.close()

    async def async_delete(self, history_id: int) -> None:
        """在一个异步事务中删除指定下载历史。"""
        async with self._async_session() as session:
            unit_of_work = SqlAlchemyAsyncUnitOfWork(session)
            try:
                await DownloadHistoryOper(session).async_delete_history(history_id)
                await unit_of_work.commit()
            except Exception:
                await unit_of_work.rollback()
                raise


class SessionDownloadHistoryRepository:
    """把 API 请求持有的 Session 适配为下载历史查询和暂存端口。"""

    def __init__(self, session: Union[Session, AsyncSession]) -> None:
        """保存由请求依赖独占的数据库 Session。"""
        self._session = session

    async def async_list_by_page(
        self,
        page: int = 1,
        count: int = 30,
    ) -> list[DownloadHistorySnapshot]:
        """在请求异步 Session 内分页读取并投影历史。"""
        if not isinstance(self._session, AsyncSession):
            raise RuntimeError("下载历史异步查询需要 AsyncSession")
        records = await DownloadHistoryOper(self._session).async_list_by_page(
            page,
            count,
        )
        return [_project_history(record) for record in records]

    def stage_delete_history(self, history_id: int) -> None:
        """在请求同步 Session 内暂存下载历史删除。"""
        if not isinstance(self._session, Session):
            raise RuntimeError("下载历史同步删除需要 Session")
        DownloadHistoryOper(self._session).stage_delete_history(history_id)

    def stage_delete_file_by_fullpath(self, fullpath: str) -> None:
        """在请求同步 Session 内暂存下载文件失效状态。"""
        if not isinstance(self._session, Session):
            raise RuntimeError("下载文件同步变更需要 Session")
        DownloadHistoryOper(self._session).stage_delete_file_by_fullpath(fullpath)
