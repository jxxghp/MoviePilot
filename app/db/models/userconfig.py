from sqlalchemy import Column, String, UniqueConstraint, Index, JSON
from sqlalchemy.orm import Session

from app.db import db_query, db_update, get_id_column, Base


class UserConfig(Base):
    """
    用户配置表
    """
    id = get_id_column()
    # 用户名
    username = Column(String, index=True)
    # 配置键
    key = Column(String)
    # 值
    value = Column(JSON)

    __table_args__ = (
        # 用户名和配置键联合唯一
        UniqueConstraint('username', 'key'),
        Index('ix_userconfig_username_key', 'username', 'key'),
    )

    @classmethod
    @db_query
    def get_by_key(cls, db: Session, username: str, key: str):
        return db.query(cls) \
                 .filter(cls.username == username) \
                 .filter(cls.key == key) \
                 .first()

    @db_update
    def delete_by_key(self, db: Session, username: str, key: str):
        userconfig = self.get_by_key(db=db, username=username, key=key)
        if userconfig:
            userconfig.delete(db=db, rid=userconfig.id)
        return True
