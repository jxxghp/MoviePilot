import time

from sqlalchemy import Column, Integer, String, Sequence, Boolean
from sqlalchemy.orm import Session

from app.db.models import Base


class TransferHistory(Base):
    """
    转移历史记录
    """
    id = Column(Integer, Sequence('id'), primary_key=True, index=True)
    # 源目录
    src = Column(String, index=True)
    # 目标目录
    dest = Column(String)
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
    tmdbid = Column(Integer)
    imdbid = Column(String)
    tvdbid = Column(Integer)
    doubanid = Column(String)
    # Sxx
    seasons = Column(Integer)
    # Exx
    episodes = Column(String)
    # 海报
    image = Column(String)
    # 下载器hash
    download_hash = Column(String, index=True)
    # 转移成功状态
    status = Column(Boolean(), default=True)
    # 转移失败信息
    errmsg = Column(String)
    # 时间
    date = Column(String, index=True)

    @staticmethod
    def list_by_title(db: Session, title: str, page: int = 1, count: int = 30):
        return db.query(TransferHistory).filter(TransferHistory.title == title).order_by(
            TransferHistory.date.desc()).offset((page - 1) * count).limit(
            count).all()

    @staticmethod
    def list_by_page(db: Session, page: int = 1, count: int = 30):
        return db.query(TransferHistory).order_by(TransferHistory.date.desc()).offset((page - 1) * count).limit(
            count).all()

    @staticmethod
    def get_by_hash(db: Session, download_hash: str):
        return db.query(TransferHistory).filter(TransferHistory.download_hash == download_hash).first()
