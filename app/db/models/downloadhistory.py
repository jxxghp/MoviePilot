import time

from sqlalchemy import Column, Integer, String, Sequence
from sqlalchemy.orm import Session

from app.db import db_query, db_update, Base


class DownloadHistory(Base):
    """
    下载历史记录
    """
    id = Column(Integer, Sequence('id'), primary_key=True, index=True)
    # 保存路径
    path = Column(String, nullable=False, index=True)
    # 类型 电影/电视剧
    type = Column(String, nullable=False)
    # 标题
    title = Column(String, nullable=False)
    # 年份
    year = Column(String)
    tmdbid = Column(Integer, index=True)
    imdbid = Column(String)
    tvdbid = Column(Integer)
    doubanid = Column(String)
    # Sxx
    seasons = Column(String)
    # Exx
    episodes = Column(String)
    # 海报
    image = Column(String)
    # 下载任务Hash
    download_hash = Column(String, index=True)
    # 种子名称
    torrent_name = Column(String)
    # 种子描述
    torrent_description = Column(String)
    # 种子站点
    torrent_site = Column(String)
    # 下载用户
    userid = Column(String)
    # 下载用户名/插件名
    username = Column(String)
    # 下载渠道
    channel = Column(String)
    # 创建时间
    date = Column(String)
    # 附加信息
    note = Column(String)

    @staticmethod
    @db_query
    def get_by_hash(db: Session, download_hash: str):
        return db.query(DownloadHistory).filter(DownloadHistory.download_hash == download_hash).first()

    @staticmethod
    @db_query
    def list_by_page(db: Session, page: int = 1, count: int = 30):
        result = db.query(DownloadHistory).offset((page - 1) * count).limit(count).all()
        return list(result)

    @staticmethod
    @db_query
    def get_by_path(db: Session, path: str):
        return db.query(DownloadHistory).filter(DownloadHistory.path == path).first()

    @staticmethod
    @db_query
    def get_last_by(db: Session, mtype: str = None, title: str = None, year: int = None, season: str = None,
                    episode: str = None, tmdbid: int = None):
        """
        据tmdbid、season、season_episode查询转移记录
        """
        result = None
        if tmdbid and not season and not episode:
            result = db.query(DownloadHistory).filter(DownloadHistory.tmdbid == tmdbid).order_by(
                DownloadHistory.id.desc()).all()
        if tmdbid and season and not episode:
            result = db.query(DownloadHistory).filter(DownloadHistory.tmdbid == tmdbid,
                                                      DownloadHistory.seasons == season).order_by(
                DownloadHistory.id.desc()).all()
        if tmdbid and season and episode:
            result = db.query(DownloadHistory).filter(DownloadHistory.tmdbid == tmdbid,
                                                      DownloadHistory.seasons == season,
                                                      DownloadHistory.episodes == episode).order_by(
                DownloadHistory.id.desc()).all()
        # 电视剧所有季集｜电影
        if not season and not episode:
            result = db.query(DownloadHistory).filter(DownloadHistory.type == mtype,
                                                      DownloadHistory.title == title,
                                                      DownloadHistory.year == year).order_by(
                DownloadHistory.id.desc()).all()
        # 电视剧某季
        if season and not episode:
            result = db.query(DownloadHistory).filter(DownloadHistory.type == mtype,
                                                      DownloadHistory.title == title,
                                                      DownloadHistory.year == year,
                                                      DownloadHistory.seasons == season).order_by(
                DownloadHistory.id.desc()).all()
        # 电视剧某季某集
        if season and episode:
            result = db.query(DownloadHistory).filter(DownloadHistory.type == mtype,
                                                      DownloadHistory.title == title,
                                                      DownloadHistory.year == year,
                                                      DownloadHistory.seasons == season,
                                                      DownloadHistory.episodes == episode).order_by(
                DownloadHistory.id.desc()).all()

        if result:
            return list(result)

    @staticmethod
    @db_query
    def list_by_user_date(db: Session, date: str, username: str = None):
        """
        查询某用户某时间之后的下载历史
        """
        if username:
            result = db.query(DownloadHistory).filter(DownloadHistory.date < date,
                                                      DownloadHistory.username == username).order_by(
                DownloadHistory.id.desc()).all()
        else:
            result = db.query(DownloadHistory).filter(DownloadHistory.date < date).order_by(
                DownloadHistory.id.desc()).all()
        return list(result)

    @staticmethod
    @db_query
    def list_by_date(db: Session, date: str, type: str, tmdbid: str, seasons: str = None):
        """
        查询某时间之后的下载历史
        """
        if seasons:
            return db.query(DownloadHistory).filter(DownloadHistory.date > date,
                                                    DownloadHistory.type == type,
                                                    DownloadHistory.tmdbid == tmdbid,
                                                    DownloadHistory.seasons == seasons).order_by(
                DownloadHistory.id.desc()).all()
        else:
            return db.query(DownloadHistory).filter(DownloadHistory.date > date,
                                                    DownloadHistory.type == type,
                                                    DownloadHistory.tmdbid == tmdbid).order_by(
                DownloadHistory.id.desc()).all()

    @staticmethod
    @db_query
    def list_by_type(db: Session, mtype: str, days: int):
        result = db.query(DownloadHistory) \
            .filter(DownloadHistory.type == mtype,
                    DownloadHistory.date >= time.strftime("%Y-%m-%d %H:%M:%S",
                                                          time.localtime(time.time() - 86400 * int(days)))
                    ).all()
        return list(result)


class DownloadFiles(Base):
    """
    下载文件记录
    """
    id = Column(Integer, Sequence('id'), primary_key=True, index=True)
    # 下载任务Hash
    download_hash = Column(String, index=True)
    # 下载器
    downloader = Column(String)
    # 完整路径
    fullpath = Column(String, index=True)
    # 保存路径
    savepath = Column(String, index=True)
    # 文件相对路径/名称
    filepath = Column(String)
    # 种子名称
    torrentname = Column(String)
    # 状态 0-已删除 1-正常
    state = Column(Integer, nullable=False, default=1)

    @staticmethod
    @db_query
    def get_by_hash(db: Session, download_hash: str, state: int = None):
        if state:
            result = db.query(DownloadFiles).filter(DownloadFiles.download_hash == download_hash,
                                                    DownloadFiles.state == state).all()
        else:
            result = db.query(DownloadFiles).filter(DownloadFiles.download_hash == download_hash).all()

        return list(result)

    @staticmethod
    @db_query
    def get_by_fullpath(db: Session, fullpath: str, all_files: bool = False):
        if not all_files:
            return db.query(DownloadFiles).filter(DownloadFiles.fullpath == fullpath).order_by(
                DownloadFiles.id.desc()).first()
        else:
            return db.query(DownloadFiles).filter(DownloadFiles.fullpath == fullpath).order_by(
                DownloadFiles.id.desc()).all()

    @staticmethod
    @db_query
    def get_by_savepath(db: Session, savepath: str):
        result = db.query(DownloadFiles).filter(DownloadFiles.savepath == savepath).all()
        return list(result)

    @staticmethod
    @db_update
    def delete_by_fullpath(db: Session, fullpath: str):
        db.query(DownloadFiles).filter(DownloadFiles.fullpath == fullpath,
                                       DownloadFiles.state == 1).update(
            {
                "state": 0
            }
        )
