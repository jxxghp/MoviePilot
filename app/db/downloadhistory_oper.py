from pathlib import Path
from typing import Any

from app.db import DbOper
from app.db.models.downloadhistory import DownloadHistory


class DownloadHistoryOper(DbOper):
    """
    下载历史管理
    """

    def get_by_path(self, path: Path) -> Any:
        """
        按路径查询下载记录
        :param path: 数据key
        """
        return DownloadHistory.get_by_path(self._db, str(path))

    def get_by_hash(self, download_hash: str) -> Any:
        """
        按Hash查询下载记录
        :param download_hash: 数据key
        """
        return DownloadHistory.get_by_hash(self._db, download_hash)

    def add(self, **kwargs):
        """
        新增下载历史
        """
        downloadhistory = DownloadHistory(**kwargs)
        return downloadhistory.create(self._db)

    def list_by_page(self, page: int = 1, count: int = 30):
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
                    season: str = None, episode: str = None, tmdbid=None):
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
