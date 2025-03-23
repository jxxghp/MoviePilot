import time
from typing import Optional

from sqlalchemy import Column, Integer, String, Sequence, Boolean, func, or_, JSON
from sqlalchemy.orm import Session

from app.db import db_query, db_update, Base


class TransferHistory(Base):
    """
    整理记录
    """
    id = Column(Integer, Sequence('id'), primary_key=True, index=True)
    # 源路径
    src = Column(String, index=True)
    # 源存储
    src_storage = Column(String)
    # 源文件项
    src_fileitem = Column(JSON, default=dict)
    # 目标路径
    dest = Column(String)
    # 目标存储
    dest_storage = Column(String)
    # 目标文件项
    dest_fileitem = Column(JSON, default=dict)
    # 转移模式 move/copy/link...
    mode = Column(String)
    # 类型 电影/电视剧
    type = Column(String)
    # 二级分类
    category = Column(String)
    # 标题
    title = Column(String, index=True)
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
    # 下载器
    downloader = Column(String)
    # 下载器hash
    download_hash = Column(String, index=True)
    # 转移成功状态
    status = Column(Boolean(), default=True)
    # 转移失败信息
    errmsg = Column(String)
    # 时间
    date = Column(String, index=True)
    # 文件清单，以JSON存储
    files = Column(JSON, default=list)

    @staticmethod
    @db_query
    def list_by_title(db: Session, title: str, page: Optional[int] = 1, count: Optional[int] = 30, status: bool = None):
        if status is not None:
            result = db.query(TransferHistory).filter(
                TransferHistory.status == status
            ).order_by(
                TransferHistory.date.desc()
            ).offset((page - 1) * count).limit(count).all()
        else:
            result = db.query(TransferHistory).filter(or_(
                TransferHistory.title.like(f'%{title}%'),
                TransferHistory.src.like(f'%{title}%'),
                TransferHistory.dest.like(f'%{title}%'),
            )).order_by(
                TransferHistory.date.desc()
            ).offset((page - 1) * count).limit(count).all()
        return list(result)

    @staticmethod
    @db_query
    def list_by_page(db: Session, page: Optional[int] = 1, count: Optional[int] = 30, status: bool = None):
        if status is not None:
            result = db.query(TransferHistory).filter(
                TransferHistory.status == status
            ).order_by(
                TransferHistory.date.desc()
            ).offset((page - 1) * count).limit(count).all()
        else:
            result = db.query(TransferHistory).order_by(
                TransferHistory.date.desc()
            ).offset((page - 1) * count).limit(count).all()
        return list(result)

    @staticmethod
    @db_query
    def get_by_hash(db: Session, download_hash: str):
        return db.query(TransferHistory).filter(TransferHistory.download_hash == download_hash).first()

    @staticmethod
    @db_query
    def get_by_src(db: Session, src: str, storage: Optional[str] = None):
        if storage:
            return db.query(TransferHistory).filter(TransferHistory.src == src,
                                                    TransferHistory.src_storage == storage).first()
        else:
            return db.query(TransferHistory).filter(TransferHistory.src == src).first()

    @staticmethod
    @db_query
    def get_by_dest(db: Session, dest: str):
        return db.query(TransferHistory).filter(TransferHistory.dest == dest).first()

    @staticmethod
    @db_query
    def list_by_hash(db: Session, download_hash: str):
        result = db.query(TransferHistory).filter(TransferHistory.download_hash == download_hash).all()
        return list(result)

    @staticmethod
    @db_query
    def statistic(db: Session, days: Optional[int] = 7):
        """
        统计最近days天的下载历史数量，按日期分组返回每日数量
        """
        sub_query = db.query(func.substr(TransferHistory.date, 1, 10).label('date'),
                             TransferHistory.id.label('id')).filter(
            TransferHistory.date >= time.strftime("%Y-%m-%d %H:%M:%S",
                                                  time.localtime(time.time() - 86400 * days))).subquery()
        result = db.query(sub_query.c.date, func.count(sub_query.c.id)).group_by(sub_query.c.date).all()
        return list(result)

    @staticmethod
    @db_query
    def count(db: Session, status: bool = None):
        if status is not None:
            return db.query(func.count(TransferHistory.id)).filter(TransferHistory.status == status).first()[0]
        else:
            return db.query(func.count(TransferHistory.id)).first()[0]

    @staticmethod
    @db_query
    def count_by_title(db: Session, title: str, status: bool = None):
        if status is not None:
            return db.query(func.count(TransferHistory.id)).filter(TransferHistory.status == status).first()[0]
        else:
            return db.query(func.count(TransferHistory.id)).filter(or_(
                TransferHistory.title.like(f'%{title}%'),
                TransferHistory.src.like(f'%{title}%'),
                TransferHistory.dest.like(f'%{title}%')
            )).first()[0]

    @staticmethod
    @db_query
    def list_by(db: Session, mtype: Optional[str] = None, title: Optional[str] = None, year: Optional[str] = None, season: Optional[str] = None,
                episode: Optional[str] = None, tmdbid: Optional[int] = None, dest: Optional[str] = None):
        """
        据tmdbid、season、season_episode查询转移记录
        tmdbid + mtype 或 title + year 必输
        """
        result = None
        # TMDBID + 类型
        if tmdbid and mtype:
            # 电视剧某季某集
            if season and episode:
                result = db.query(TransferHistory).filter(TransferHistory.tmdbid == tmdbid,
                                                          TransferHistory.type == mtype,
                                                          TransferHistory.seasons == season,
                                                          TransferHistory.episodes == episode,
                                                          TransferHistory.dest == dest).all()
            # 电视剧某季
            elif season:
                result = db.query(TransferHistory).filter(TransferHistory.tmdbid == tmdbid,
                                                          TransferHistory.type == mtype,
                                                          TransferHistory.seasons == season).all()
            else:
                if dest:
                    # 电影
                    result = db.query(TransferHistory).filter(TransferHistory.tmdbid == tmdbid,
                                                              TransferHistory.type == mtype,
                                                              TransferHistory.dest == dest).all()
                else:
                    # 电视剧所有季集
                    result = db.query(TransferHistory).filter(TransferHistory.tmdbid == tmdbid,
                                                              TransferHistory.type == mtype).all()
        # 标题 + 年份
        elif title and year:
            # 电视剧某季某集
            if season and episode:
                result = db.query(TransferHistory).filter(TransferHistory.title == title,
                                                          TransferHistory.year == year,
                                                          TransferHistory.seasons == season,
                                                          TransferHistory.episodes == episode,
                                                          TransferHistory.dest == dest).all()
            # 电视剧某季
            elif season:
                result = db.query(TransferHistory).filter(TransferHistory.title == title,
                                                          TransferHistory.year == year,
                                                          TransferHistory.seasons == season).all()
            else:
                if dest:
                    # 电影
                    result = db.query(TransferHistory).filter(TransferHistory.title == title,
                                                              TransferHistory.year == year,
                                                              TransferHistory.dest == dest).all()
                else:
                    # 电视剧所有季集
                    result = db.query(TransferHistory).filter(TransferHistory.title == title,
                                                              TransferHistory.year == year).all()
        # 类型 + 转移路径（emby webhook season无tmdbid场景）
        elif mtype and season and dest:
            # 电视剧某季
            result = db.query(TransferHistory).filter(TransferHistory.type == mtype,
                                                      TransferHistory.seasons == season,
                                                      TransferHistory.dest.like(f"{dest}%")).all()

        if result:
            return list(result)
        return []

    @staticmethod
    @db_query
    def get_by_type_tmdbid(db: Session, mtype: Optional[str] = None, tmdbid: Optional[int] = None):
        """
        据tmdbid、type查询转移记录
        """
        return db.query(TransferHistory).filter(TransferHistory.tmdbid == tmdbid,
                                                TransferHistory.type == mtype).first()

    @staticmethod
    @db_update
    def update_download_hash(db: Session, historyid: Optional[int] = None, download_hash: Optional[str] = None):
        db.query(TransferHistory).filter(TransferHistory.id == historyid).update(
            {
                "download_hash": download_hash
            }
        )

    @staticmethod
    @db_query
    def list_by_date(db: Session, date: str):
        """
        查询某时间之后的转移历史
        """
        return db.query(TransferHistory).filter(TransferHistory.date > date).order_by(TransferHistory.id.desc()).all()
