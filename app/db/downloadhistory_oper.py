from pathlib import Path
from typing import List

from app.db import DbOper
from app.db.models.downloadhistory import DownloadHistory, DownloadFiles


class DownloadHistoryOper(DbOper):
    """
    下载历史管理
    """

    def get_by_path(self, path: Path) -> DownloadHistory:
        """
        按路径查询下载记录
        :param path: 数据key
        """
        return DownloadHistory.get_by_path(self._db, str(path))

    def get_by_hash(self, download_hash: str) -> DownloadHistory:
        """
        按Hash查询下载记录
        :param download_hash: 数据key
        """
        return DownloadHistory.get_by_hash(self._db, download_hash)

    def add(self, **kwargs) -> DownloadHistory:
        """
        新增下载历史
        """
        downloadhistory = DownloadHistory(**kwargs)
        return downloadhistory.create(self._db)

    def add_files(self, file_items: List[dict]):
        """
        新增下载历史文件
        """
        for file_item in file_items:
            downloadfile = DownloadFiles(**file_item)
            downloadfile.create(self._db)

    def get_files_by_hash(self, download_hash: str, state: int = None) -> List[DownloadFiles]:
        """
        按Hash查询下载文件记录
        :param download_hash: 数据key
        :param state: 删除状态
        """
        return DownloadFiles.get_by_hash(self._db, download_hash, state)

    def get_file_by_fullpath(self, fullpath: str) -> DownloadFiles:
        """
        按fullpath查询下载文件记录
        :param fullpath: 数据key
        """
        return DownloadFiles.get_by_fullpath(self._db, fullpath)

    def get_files_by_savepath(self, fullpath: str) -> List[DownloadFiles]:
        """
        按savepath查询下载文件记录
        :param fullpath: 数据key
        """
        return DownloadFiles.get_by_savepath(self._db, fullpath)

    def delete_file_by_fullpath(self, fullpath: str):
        """
        按fullpath删除下载文件记录
        :param fullpath: 数据key
        """
        DownloadFiles.delete_by_fullpath(self._db, fullpath)

    def list_by_page(self, page: int = 1, count: int = 30) -> List[DownloadHistory]:
        """
        分页查询下载历史
        """
        return DownloadHistory.list_by_page(self._db, page, count)

    def truncate(self):
        """
        清空下载记录
        """
        DownloadHistory.truncate(self._db)

    def get_last_by(self, mtype=None, title: str = None, year: str = None,
                    season: str = None, episode: str = None, tmdbid=None) -> List[DownloadHistory]:
        """
        按类型、标题、年份、季集查询下载记录
        """
        return DownloadHistory.get_last_by(db=self._db,
                                           mtype=mtype,
                                           title=title,
                                           year=year,
                                           season=season,
                                           episode=episode,
                                           tmdbid=tmdbid)
