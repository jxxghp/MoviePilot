from sqlalchemy import Column, Integer, String, Sequence
from sqlalchemy.orm import Session

from app.db.models import Base


class SystemConfig(Base):
    """
    配置表
    """
    id = Column(Integer, Sequence('id'), primary_key=True, index=True)
    key = Column(String, index=True)
    value = Column(String, nullable=True)

    @staticmethod
    def get_by_key(db: Session, key: str):
        return db.query(SystemConfig).filter(SystemConfig.key == key).first()

    @staticmethod
    def delete_by_key(db: Session, key: str):
        db.query(SystemConfig).filter(SystemConfig.key == key).delete()
        db.commit()
