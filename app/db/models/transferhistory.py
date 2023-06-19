import time

from sqlalchemy import Column, Integer, String, Sequence
from sqlalchemy.orm import Session

from app.db.models import Base


class TransferHistory(Base):
    """
    转移历史记录
    """
    id = Column(Integer, Sequence('id'), primary_key=True, index=True)
    src = Column(String, index=True)
    dest = Column(String)
    mode = Column(String)
    type = Column(String)
    category = Column(String)
    title = Column(String, index=True)
    year = Column(String)
    tmdbid = Column(Integer)
    imdbid = Column(String)
    tvdbid = Column(Integer)
    doubanid = Column(String)
    seasons = Column(Integer)
    episodes = Column(String)
    image = Column(String)
    download_hash = Column(String)
    date = Column(String, index=True, default=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()))

    @staticmethod
    def search_by_title(db: Session, title: str):
        return db.query(TransferHistory).filter(TransferHistory.title == title).all()
