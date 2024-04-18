from sqlalchemy import Column, Integer, String, Sequence, UniqueConstraint, Index
from sqlalchemy.orm import Session

from app.db import db_query, db_update, Base


class UserConfig(Base):
    """
    用户配置表
    """
    id = Column(Integer, Sequence('id'), primary_key=True, index=True)
    # 用户ID
    user_id = Column(Integer, index=True)
    # 配置键
    key = Column(String)
    # 值
    value = Column(String, nullable=True)

    __table_args__ = (
        # 用户ID和配置键联合唯一
        UniqueConstraint('user_id', 'key'),
        Index('ix_userconfig_userid_key', 'user_id', 'key'),
    )

    @staticmethod
    @db_query
    def get_by_key(db: Session, user_id: int, key: str):
        return db.query(UserConfig) \
                 .filter(UserConfig.user_id == user_id) \
                 .filter(UserConfig.key == key) \
                 .first()

    @db_update
    def delete_by_key(self, db: Session, user_id: int, key: str):
        userconfig = self.get_by_key(db, user_id, key)
        if userconfig:
            userconfig.delete(db, userconfig.id)
        return True
