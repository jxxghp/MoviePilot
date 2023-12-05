from sqlalchemy import Column, Integer, String, Sequence
from sqlalchemy.orm import Session

from app.db import db_query, db_update, Base


class SystemConfig(Base):
    """
    配置表
    """
    id = Column(Integer, Sequence('id'), primary_key=True, index=True)
    # 主键
    key = Column(String, index=True)
    # 值
    value = Column(String, nullable=True)

    @staticmethod
    @db_query
    def get_by_key(db: Session, key: str):
        return db.query(SystemConfig).filter(SystemConfig.key == key).first()

    @db_update
    def delete_by_key(self, db: Session, key: str):
        systemconfig = self.get_by_key(db, key)
        if systemconfig:
            systemconfig.delete(db, systemconfig.id)
        return True
