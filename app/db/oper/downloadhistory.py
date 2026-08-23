from typing import Dict, List, Optional, cast

from sqlalchemy import delete as sqlalchemy_delete, update as sqlalchemy_update
from sqlalchemy.orm import Session

from app.db.base import DbOper
from app.db.models.downloadhistory import DownloadHistory, DownloadFiles
from app.schemas.types import MediaSource


class DownloadHistoryOper(DbOper):
    """
    下载历史管理
    """

    def get_by_path(self, path: str) -> Optional[DownloadHistory]:
        """
        按路径查询下载记录
        :param path: 数据key
        """
        return self._execute_sync_query(
            lambda session: DownloadHistory.get_by_path(session, path)
        )

    def get_by_hash(self, download_hash: str) -> Optional[DownloadHistory]:
        """
        按Hash查询下载记录
        :param download_hash: 数据key
        """
        return self._execute_sync_query(
            lambda session: DownloadHistory.get_by_hash(session, download_hash)
        )

    def get_by_hashes(self, download_hashes: List[str]) -> Dict[str, DownloadHistory]:
        """
        批量按 Hash 查询下载记录，并返回以 Hash 为键的映射。
        """
        histories = self._execute_sync_query(
            lambda session: DownloadHistory.get_by_hashes(
                session, download_hashes
            )
        )
        return {
            history.download_hash: history
            for history in histories
            if history and history.download_hash
        }

    def get_by_media_identity(
            self, media_source: MediaSource, media_id: str,
            music_type: Optional[str] = None,
    ) -> List[DownloadHistory]:
        """
        按规范媒体身份查询下载记录。
        :param media_source: 媒体数据源
        :param media_id: 数据源原生 ID
        :param music_type: 音乐实体类型
        """
        return self._execute_sync_query(
            lambda session: DownloadHistory.get_by_media_identity(
                session,
                media_source=media_source,
                media_id=media_id,
                music_type=music_type,
            )
        )

    def add(self, **kwargs):
        """
        新增下载历史
        """
        self._stage_create(DownloadHistory(**kwargs))

    def stage_add(self, payload: dict) -> DownloadHistory:
        """在调用方同步 Session 中暂存下载历史并返回已分配 ID 的记录。"""
        if not isinstance(self._db, Session):
            raise RuntimeError("下载历史事务写入需要调用方提供同步 Session")
        history = DownloadHistory(**payload)
        self._db.add(history)
        self._db.flush()
        return history

    def add_files(self, file_items: List[dict]):
        """
        新增下载历史文件
        """
        for file_item in file_items:
            downloadfile = DownloadFiles(**file_item)
            self._stage_create(downloadfile)

    def stage_add_files(self, file_items: List[dict]) -> None:
        """在调用方事务内批量暂存下载文件，不逐条提交。"""
        if not isinstance(self._db, Session):
            raise RuntimeError("下载文件事务写入需要调用方提供同步 Session")
        self._db.add_all(DownloadFiles(**item) for item in file_items)
        self._db.flush()

    def truncate_files(self):
        """
        清空下载历史文件记录
        """
        self._stage_truncate(DownloadFiles)

    def get_files_by_hash(self, download_hash: str, state: Optional[int] = None) -> List[DownloadFiles]:
        """
        按Hash查询下载文件记录
        :param download_hash: 数据key
        :param state: 删除状态
        """
        return self._execute_sync_query(
            lambda session: DownloadFiles.get_by_hash(
                session, download_hash, state
            )
        )

    def get_file_by_fullpath(self, fullpath: str) -> Optional[DownloadFiles]:
        """
        按fullpath查询下载文件记录
        :param fullpath: 数据key
        """
        return self._execute_sync_query(
            lambda session: cast(
                Optional[DownloadFiles],
                DownloadFiles.get_by_fullpath(
                    session, fullpath=fullpath, all_files=False
                ),
            )
        )

    def get_files_by_fullpath(self, fullpath: str) -> List[DownloadFiles]:
        """
        按fullpath查询下载文件记录
        :param fullpath: 数据key
        """
        return self._execute_sync_query(
            lambda session: cast(
                List[DownloadFiles],
                DownloadFiles.get_by_fullpath(
                    session, fullpath=fullpath, all_files=True
                ),
            )
        )

    def get_files_by_savepath(self, fullpath: str) -> List[DownloadFiles]:
        """
        按savepath查询下载文件记录
        :param fullpath: 数据key
        """
        return self._execute_sync_query(
            lambda session: DownloadFiles.get_by_savepath(session, fullpath)
        )

    def delete_file_by_fullpath(self, fullpath: str):
        """
        按fullpath删除下载文件记录
        :param fullpath: 数据key
        """
        self._execute_sync_write(
            lambda session: DownloadFiles.delete_by_fullpath(session, fullpath)
        )

    def stage_delete_file_by_fullpath(self, fullpath: str) -> None:
        """暂存指定完整路径的下载文件记录删除。"""
        self._db.execute(
            sqlalchemy_update(DownloadFiles)
            .where(
                DownloadFiles.fullpath == fullpath,
                DownloadFiles.state == 1,
            )
            .values(state=0)
        )

    def get_hash_by_fullpath(self, fullpath: str) -> Optional[str]:
        """
        按fullpath查询下载文件记录hash
        :param fullpath: 数据key
        """
        fileinfo = self._execute_sync_query(
            lambda session: cast(
                Optional[DownloadFiles],
                DownloadFiles.get_by_fullpath(
                    session, fullpath=fullpath, all_files=False
                ),
            )
        )
        if fileinfo:
            return fileinfo.download_hash
        return ""

    def list_by_page(self, page: int = 1, count: int = 30) -> List[DownloadHistory]:
        """
        分页查询下载历史
        """
        return self._execute_sync_query(
            lambda session: DownloadHistory.list_by_page(session, page, count)
        )

    async def async_list_by_page(
        self,
        page: int = 1,
        count: int = 30,
    ) -> List[DownloadHistory]:
        """异步分页查询下载历史。"""
        return await self._execute_async_query(
            lambda session: DownloadHistory.async_list_by_page(
                session, page, count
            )
        )

    async def async_delete_history(self, historyid: int):
        """
        异步删除下载记录。
        """
        await self._stage_async_delete(DownloadHistory, historyid)

    def truncate(self):
        """
        清空下载记录
        """
        self._stage_truncate(DownloadHistory)

    def get_last_by(self, mtype=None, title: Optional[str] = None, year: Optional[str] = None,
                    season: Optional[str] = None, episode: Optional[str] = None,
                    media_source: Optional[MediaSource] = None,
                    media_id: Optional[str] = None) -> List[DownloadHistory]:
        """
        按类型、标题、年份、季集查询下载记录
        媒体身份 + mtype 或 title + year
        """
        return self._execute_sync_query(
            lambda session: DownloadHistory.get_last_by(
                db=session,
                mtype=mtype,
                title=title,
                year=year,
                season=season,
                episode=episode,
                media_source=media_source,
                media_id=media_id,
            )
        )

    def list_by_user_date(self, date: str, username: Optional[str] = None) -> List[DownloadHistory]:
        """
        查询某用户某时间之前的下载历史
        """
        return self._execute_sync_query(
            lambda session: DownloadHistory.list_by_user_date(
                db=session,
                date=date,
                username=username,
            )
        )

    def list_by_date(
            self, date: str, type: str, media_source: MediaSource, media_id: str,
            seasons: Optional[str] = None,
    ) -> List[DownloadHistory]:
        """
        查询某时间之后的下载历史
        """
        return self._execute_sync_query(
            lambda session: DownloadHistory.list_by_date(
                db=session,
                date=date,
                type=type,
                media_source=media_source,
                media_id=media_id,
                seasons=seasons,
            )
        )

    def list_by_type(self, mtype: str, days: int = 7) -> List[DownloadHistory]:
        """
        获取指定类型的下载历史
        """
        return self._execute_sync_query(
            lambda session: DownloadHistory.list_by_type(
                db=session,
                mtype=mtype,
                days=days,
            )
        )

    def delete_history(self, historyid):
        """
        删除下载记录
        """
        self._stage_delete(DownloadHistory, historyid)

    def stage_delete_history(self, historyid: int) -> None:
        """暂存下载记录删除，不由模型装饰器提交事务。"""
        self._db.execute(
            sqlalchemy_delete(DownloadHistory).where(
                DownloadHistory.id == historyid
            )
        )

    def delete_downloadfile(self, downloadfileid):
        """
        删除下载文件记录
        """
        self._stage_delete(DownloadFiles, downloadfileid)
