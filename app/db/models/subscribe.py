from sqlalchemy import Column, Integer, String, Sequence
from sqlalchemy.orm import Session

from app.db.models import Base


class Subscribe(Base):
    """
    订阅表
    """
    id = Column(Integer, Sequence('id'), primary_key=True, index=True)
    name = Column(String, nullable=False, index=True)
    year = Column(String)
    type = Column(String)
    keyword = Column(String)
    tmdbid = Column(Integer, index=True)
    tvdbid = Column(Integer, index=True)
    doubanid = Column(String)
    season = Column(Integer)
    image = Column(String)
    description = Column(String)
    filter = Column(String)
    include = Column(String)
    exclude = Column(String)
    total_episode = Column(Integer)
    start_episode = Column(Integer)
    lack_episode = Column(Integer)
    note = Column(String)
    state = Column(String, nullable=False, index=True, default='N')

    @staticmethod
    def exists(db: Session, tmdbid: int, season: int = None):
        if season:
            return db.query(Subscribe).filter(Subscribe.tmdbid == tmdbid,
                                              Subscribe.season == season).first()
        return db.query(Subscribe).filter(Subscribe.tmdbid == tmdbid).first()

    @staticmethod
    def get_by_state(db: Session, state: str):
        return db.query(Subscribe).filter(Subscribe.state == state).all()

    @staticmethod
    def get_by_tmdbid(db: Session, tmdbid: int):
        return db.query(Subscribe).filter(Subscribe.tmdbid == tmdbid).first()

    @staticmethod
    def get_by_title(db: Session, title: str):
        return db.query(Subscribe).filter(Subscribe.name == title).first()

    @staticmethod
    def get_by_tvdbid(db: Session, tvdbid: int):
        return db.query(Subscribe).filter(Subscribe.tvdbid == tvdbid).first()
