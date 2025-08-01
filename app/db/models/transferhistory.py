import time
from typing import Optional

from sqlalchemy import Column, Integer, String, Sequence, Boolean, func, or_, JSON, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.db import db_query, db_update, Base, async_db_query


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
    # 剧集组
    episode_group = Column(String)

    @classmethod
    @db_query
    def list_by_title(cls, db: Session, title: str, page: Optional[int] = 1, count: Optional[int] = 30,
                      status: bool = None):
        if status is not None:
            return db.query(cls).filter(
                cls.status == status
            ).order_by(
                cls.date.desc()
            ).offset((page - 1) * count).limit(count).all()
        else:
            return db.query(cls).filter(or_(
                cls.title.like(f'%{title}%'),
                cls.src.like(f'%{title}%'),
                cls.dest.like(f'%{title}%'),
            )).order_by(
                cls.date.desc()
            ).offset((page - 1) * count).limit(count).all()

    @classmethod
    @async_db_query
    async def async_list_by_title(cls, db: AsyncSession, title: str, page: Optional[int] = 1, count: Optional[int] = 30,
                                  status: bool = None):
        if status is not None:
            result = await db.execute(
                select(cls).filter(
                    cls.status == status
                ).order_by(
                    cls.date.desc()
                ).offset((page - 1) * count).limit(count)
            )
        else:
            result = await db.execute(
                select(cls).filter(or_(
                    cls.title.like(f'%{title}%'),
                    cls.src.like(f'%{title}%'),
                    cls.dest.like(f'%{title}%'),
                )).order_by(
                    cls.date.desc()
                ).offset((page - 1) * count).limit(count)
            )
        return result.scalars().all()

    @classmethod
    @db_query
    def list_by_page(cls, db: Session, page: Optional[int] = 1, count: Optional[int] = 30, status: bool = None):
        if status is not None:
            return db.query(cls).filter(
                cls.status == status
            ).order_by(
                cls.date.desc()
            ).offset((page - 1) * count).limit(count).all()
        else:
            return db.query(cls).order_by(
                cls.date.desc()
            ).offset((page - 1) * count).limit(count).all()

    @classmethod
    @async_db_query
    async def async_list_by_page(cls, db: AsyncSession, page: Optional[int] = 1, count: Optional[int] = 30,
                                 status: bool = None):
        if status is not None:
            result = await db.execute(
                select(cls).filter(
                    cls.status == status
                ).order_by(
                    cls.date.desc()
                ).offset((page - 1) * count).limit(count)
            )
        else:
            result = await db.execute(
                select(cls).order_by(
                    cls.date.desc()
                ).offset((page - 1) * count).limit(count)
            )
        return result.scalars().all()

    @classmethod
    @db_query
    def get_by_hash(cls, db: Session, download_hash: str):
        return db.query(cls).filter(cls.download_hash == download_hash).first()

    @classmethod
    @db_query
    def get_by_src(cls, db: Session, src: str, storage: Optional[str] = None):
        if storage:
            return db.query(cls).filter(cls.src == src,
                                        cls.src_storage == storage).first()
        else:
            return db.query(cls).filter(cls.src == src).first()

    @classmethod
    @db_query
    def get_by_dest(cls, db: Session, dest: str):
        return db.query(cls).filter(cls.dest == dest).first()

    @classmethod
    @db_query
    def list_by_hash(cls, db: Session, download_hash: str):
        return db.query(cls).filter(cls.download_hash == download_hash).all()

    @classmethod
    @db_query
    def statistic(cls, db: Session, days: Optional[int] = 7):
        """
        统计最近days天的下载历史数量，按日期分组返回每日数量
        """
        sub_query = db.query(func.substr(cls.date, 1, 10).label('date'),
                             cls.id.label('id')).filter(
            cls.date >= time.strftime("%Y-%m-%d %H:%M:%S",
                                      time.localtime(time.time() - 86400 * days))).subquery()
        return db.query(sub_query.c.date, func.count(sub_query.c.id)).group_by(sub_query.c.date).all()

    @classmethod
    @async_db_query
    async def async_statistic(cls, db: AsyncSession, days: Optional[int] = 7):
        """
        统计最近days天的下载历史数量，按日期分组返回每日数量
        """
        sub_query = select(func.substr(cls.date, 1, 10).label('date'),
                           cls.id.label('id')).filter(
            cls.date >= time.strftime("%Y-%m-%d %H:%M:%S",
                                      time.localtime(time.time() - 86400 * days))).subquery()
        result = await db.execute(
            select(sub_query.c.date, func.count(sub_query.c.id)).group_by(sub_query.c.date)
        )
        return result.scalars().all()

    @classmethod
    @db_query
    def count(cls, db: Session, status: bool = None):
        if status is not None:
            return db.query(func.count(cls.id)).filter(cls.status == status).first()[0]
        else:
            return db.query(func.count(cls.id)).first()[0]

    @classmethod
    @async_db_query
    async def async_count(cls, db: AsyncSession, status: bool = None):
        if status is not None:
            result = await db.execute(
                select(func.count(cls.id)).filter(cls.status == status)
            )
        else:
            result = await db.execute(
                select(func.count(cls.id))
            )
        return result.scalar()

    @classmethod
    @db_query
    def count_by_title(cls, db: Session, title: str, status: bool = None):
        if status is not None:
            return db.query(func.count(cls.id)).filter(cls.status == status).first()[0]
        else:
            return db.query(func.count(cls.id)).filter(or_(
                cls.title.like(f'%{title}%'),
                cls.src.like(f'%{title}%'),
                cls.dest.like(f'%{title}%')
            )).first()[0]

    @classmethod
    @async_db_query
    async def async_count_by_title(cls, db: AsyncSession, title: str, status: bool = None):
        if status is not None:
            result = await db.execute(
                select(func.count(cls.id)).filter(cls.status == status)
            )
        else:
            result = await db.execute(
                select(func.count(cls.id)).filter(or_(
                    cls.title.like(f'%{title}%'),
                    cls.src.like(f'%{title}%'),
                    cls.dest.like(f'%{title}%')
                ))
            )
        return result.scalar()

    @classmethod
    @db_query
    def list_by(cls, db: Session, mtype: Optional[str] = None, title: Optional[str] = None, year: Optional[str] = None,
                season: Optional[str] = None,
                episode: Optional[str] = None, tmdbid: Optional[int] = None, dest: Optional[str] = None):
        """
        据tmdbid、season、season_episode查询转移记录
        tmdbid + mtype 或 title + year 必输
        """
        # TMDBID + 类型
        if tmdbid and mtype:
            # 电视剧某季某集
            if season and episode:
                return db.query(cls).filter(cls.tmdbid == tmdbid,
                                            cls.type == mtype,
                                            cls.seasons == season,
                                            cls.episodes == episode,
                                            cls.dest == dest).all()
            # 电视剧某季
            elif season:
                return db.query(cls).filter(cls.tmdbid == tmdbid,
                                            cls.type == mtype,
                                            cls.seasons == season).all()
            else:
                if dest:
                    # 电影
                    return db.query(cls).filter(cls.tmdbid == tmdbid,
                                                cls.type == mtype,
                                                cls.dest == dest).all()
                else:
                    # 电视剧所有季集
                    return db.query(cls).filter(cls.tmdbid == tmdbid,
                                                cls.type == mtype).all()
        # 标题 + 年份
        elif title and year:
            # 电视剧某季某集
            if season and episode:
                return db.query(cls).filter(cls.title == title,
                                            cls.year == year,
                                            cls.seasons == season,
                                            cls.episodes == episode,
                                            cls.dest == dest).all()
            # 电视剧某季
            elif season:
                return db.query(cls).filter(cls.title == title,
                                            cls.year == year,
                                            cls.seasons == season).all()
            else:
                if dest:
                    # 电影
                    return db.query(cls).filter(cls.title == title,
                                                cls.year == year,
                                                cls.dest == dest).all()
                else:
                    # 电视剧所有季集
                    return db.query(cls).filter(cls.title == title,
                                                cls.year == year).all()
        # 类型 + 转移路径（emby webhook season无tmdbid场景）
        elif mtype and season and dest:
            # 电视剧某季
            return db.query(cls).filter(cls.type == mtype,
                                        cls.seasons == season,
                                        cls.dest.like(f"{dest}%")).all()
        return []

    @classmethod
    @db_query
    def get_by_type_tmdbid(cls, db: Session, mtype: Optional[str] = None, tmdbid: Optional[int] = None):
        """
        据tmdbid、type查询转移记录
        """
        return db.query(cls).filter(cls.tmdbid == tmdbid,
                                    cls.type == mtype).first()

    @classmethod
    @db_update
    def update_download_hash(cls, db: Session, historyid: Optional[int] = None, download_hash: Optional[str] = None):
        db.query(cls).filter(cls.id == historyid).update(
            {
                "download_hash": download_hash
            }
        )

    @classmethod
    @db_query
    def list_by_date(cls, db: Session, date: str):
        """
        查询某时间之后的转移历史
        """
        return db.query(cls).filter(cls.date > date).order_by(cls.id.desc()).all()
