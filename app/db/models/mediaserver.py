from datetime import datetime
from typing import Optional

from sqlalchemy import Column, Integer, String, Sequence, JSON
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.db import db_query, db_update, async_db_query, Base


class MediaServerItem(Base):
    """
    媒体服务器媒体条目表
    """
    id = Column(Integer, Sequence('id'), primary_key=True, index=True)
    # 服务器类型
    server = Column(String)
    # 媒体库ID
    library = Column(String)
    # ID
    item_id = Column(String, index=True)
    # 类型
    item_type = Column(String)
    # 标题
    title = Column(String, index=True)
    # 原标题
    original_title = Column(String)
    # 年份
    year = Column(String)
    # TMDBID
    tmdbid = Column(Integer, index=True)
    # IMDBID
    imdbid = Column(String, index=True)
    # TVDBID
    tvdbid = Column(String, index=True)
    # 路径
    path = Column(String)
    # 季集
    seasoninfo = Column(JSON, default=dict)
    # 备注
    note = Column(JSON)
    # 同步时间
    lst_mod_date = Column(String, default=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    @classmethod
    @db_query
    def get_by_itemid(cls, db: Session, item_id: str):
        return db.query(cls).filter(cls.item_id == item_id).first()

    @classmethod
    @db_update
    def empty(cls, db: Session, server: Optional[str] = None):
        if server is None:
            db.query(cls).delete()
        else:
            db.query(cls).filter(cls.server == server).delete()

    @classmethod
    @db_query
    def exist_by_tmdbid(cls, db: Session, tmdbid: int, mtype: str):
        return db.query(cls).filter(cls.tmdbid == tmdbid,
                                    cls.item_type == mtype).first()

    @classmethod
    @db_query
    def exists_by_title(cls, db: Session, title: str, mtype: str, year: str):
        return db.query(cls).filter(cls.title == title,
                                    cls.item_type == mtype,
                                    cls.year == str(year)).first()

    @classmethod
    @async_db_query
    async def async_get_by_itemid(cls, db: AsyncSession, item_id: str):
        result = await db.execute(select(cls).filter(cls.item_id == item_id))
        return result.scalars().first()

    @classmethod
    @async_db_query
    async def async_exist_by_tmdbid(cls, db: AsyncSession, tmdbid: int, mtype: str):
        result = await db.execute(select(cls).filter(cls.tmdbid == tmdbid,
                                                     cls.item_type == mtype))
        return result.scalars().first()

    @classmethod
    @async_db_query
    async def async_exists_by_title(cls, db: AsyncSession, title: str, mtype: str, year: str):
        result = await db.execute(select(cls).filter(cls.title == title,
                                                     cls.item_type == mtype,
                                                     cls.year == str(year)))
        return result.scalars().first()
