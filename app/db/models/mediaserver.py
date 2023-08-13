from datetime import datetime

from sqlalchemy import Column, Integer, String, Sequence
from sqlalchemy.orm import Session

from app.db.models import Base


class MediaServerItem(Base):
    """
    站点表
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
    seasoninfo = Column(String)
    # 备注
    note = Column(String)
    # 同步时间
    lst_mod_date = Column(String, default=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    @staticmethod
    def get_by_itemid(db: Session, item_id: str):
        return db.query(MediaServerItem).filter(MediaServerItem.item_id == item_id).first()

    @staticmethod
    def empty(db: Session, server: str):
        db.query(MediaServerItem).filter(MediaServerItem.server == server).delete()
        db.commit()

    @staticmethod
    def exist_by_tmdbid(db: Session, tmdbid: int, mtype: str):
        return db.query(MediaServerItem).filter(MediaServerItem.tmdbid == tmdbid,
                                                MediaServerItem.item_type == mtype).all()

    @staticmethod
    def exists_by_title(db: Session, title: str, mtype: str, year: str):
        return db.query(MediaServerItem).filter(MediaServerItem.title == title,
                                                MediaServerItem.item_type == mtype,
                                                MediaServerItem.year == str(year)).all()