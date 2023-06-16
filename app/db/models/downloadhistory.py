from sqlalchemy import Column, Integer, String, Sequence
from sqlalchemy.orm import Session

from app.db.models import Base


class DownloadHistory(Base):
    """
    下载历史记录
    """
    id = Column(Integer, Sequence('id'), primary_key=True, index=True)
    path = Column(String, nullable=False, index=True)
    type = Column(String, nullable=False)
    title = Column(String, nullable=False)
    year = Column(String)
    tmdbid = Column(Integer, index=True)
    imdbid = Column(String)
    tvdbid = Column(Integer)
    doubanid = Column(String)
    seasons = Column(Integer)
    episodes = Column(String)
    image = Column(String)
    download_hash = Column(String, index=True)
    torrent_name = Column(String)
    torrent_description = Column(String)
    torrent_site = Column(String)
    note = Column(String)

    @staticmethod
    def get_by_hash(db: Session, download_hash: str):
        return db.query(DownloadHistory).filter(DownloadHistory.download_hash == download_hash).first()
