from typing import List

from app.db import DbOper
from app.db.models.downloadhistory import DownloadHistory, DownloadFiles


class DownloadHistoryOper(DbOper):
    """
    下载历史管理
    """

    def get_by_path(self, path: str) -> DownloadHistory:
        """
        按路径查询下载记录
        :param path: 数据key
        """
        return DownloadHistory.get_by_path(self._db, path)

    def get_by_hash(self, download_hash: str) -> DownloadHistory:
        """
        按Hash查询下载记录
        :param download_hash: 数据key
        """
        return DownloadHistory.get_by_hash(self._db, download_hash)

    def add(self, **kwargs):
        """
        新增下载历史
        """
        DownloadHistory(**kwargs).create(self._db)

    def add_files(self, file_items: List[dict]):
        """
        新增下载历史文件
        """
        for file_item in file_items:
            downloadfile = DownloadFiles(**file_item)
            downloadfile.create(self._db)

    def truncate_files(self):
        """
        清空下载历史文件记录
        """
        DownloadFiles.truncate(self._db)

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
        return DownloadFiles.get_by_fullpath(self._db, fullpath=fullpath, all_files=False)

    def get_files_by_fullpath(self, fullpath: str) -> List[DownloadFiles]:
        """
        按fullpath查询下载文件记录
        :param fullpath: 数据key
        """
        return DownloadFiles.get_by_fullpath(self._db, fullpath=fullpath, all_files=True)

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

    def get_hash_by_fullpath(self, fullpath: str) -> str:
        """
        按fullpath查询下载文件记录hash
        :param fullpath: 数据key
        """
        fileinfo: DownloadFiles = DownloadFiles.get_by_fullpath(self._db, fullpath=fullpath, all_files=False)
        if fileinfo:
            return fileinfo.download_hash
        return ""

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

    def list_by_user_date(self, date: str, username: str = None) -> List[DownloadHistory]:
        """
        查询某用户某时间之前的下载历史
        """
        return DownloadHistory.list_by_user_date(db=self._db,
                                                 date=date,
                                                 username=username)

    def list_by_date(self, date: str, type: str, tmdbid: str, seasons: str = None) -> List[DownloadHistory]:
        """
        查询某时间之后的下载历史
        """
        return DownloadHistory.list_by_date(db=self._db,
                                            date=date,
                                            type=type,
                                            tmdbid=tmdbid,
                                            seasons=seasons)

    def list_by_type(self, mtype: str, days: int = 7) -> List[DownloadHistory]:
        """
        获取指定类型的下载历史
        """
        return DownloadHistory.list_by_type(db=self._db,
                                            mtype=mtype,
                                            days=days)

    def delete_history(self, historyid):
        """
        删除下载记录
        """
        DownloadHistory.delete(self._db, historyid)

    def delete_downloadfile(self, downloadfileid):
        """
        删除下载文件记录
        """
        DownloadFiles.delete(self._db, downloadfileid)
