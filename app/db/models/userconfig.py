from sqlalchemy import String, UniqueConstraint, JSON, select
from sqlalchemy.orm import Session, mapped_column

from app.db import db_query, db_update, get_id_column, Base


class UserConfig(Base):
    """
    用户配置表
    """
    id = get_id_column()
    # 用户名
    username = mapped_column(String)
    # 配置键
    key = mapped_column(String)
    # 值
    value = mapped_column(JSON)

    __table_args__ = (
        # 用户名和配置键联合唯一
        UniqueConstraint('username', 'key'),
    )

    @classmethod
    @db_query
    def get_by_key(cls, db: Session, username: str, key: str):
        return db.execute(
            select(cls).where(cls.username == username, cls.key == key)
        ).scalars().first()

    @db_update
    def delete_by_key(self, db: Session, username: str, key: str):
        userconfig = self.get_by_key(db=db, username=username, key=key)
        if userconfig:
            userconfig.delete(db=db, rid=userconfig.id)
        return True
