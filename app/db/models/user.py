from sqlalchemy import Boolean, Column, Integer, String, Sequence
from sqlalchemy.orm import Session

from app.core.security import verify_password
from app.db import db_query, db_update, Base


class User(Base):
    """
    用户表
    """
    # ID
    id = Column(Integer, Sequence('id'), primary_key=True, index=True)
    # 用户名
    name = Column(String, index=True, nullable=False)
    # 邮箱，未启用
    email = Column(String)
    # 加密后密码
    hashed_password = Column(String)
    # 是否启用
    is_active = Column(Boolean(), default=True)
    # 是否管理员
    is_superuser = Column(Boolean(), default=False)
    # 头像
    avatar = Column(String)

    @staticmethod
    @db_query
    def authenticate(db: Session, name: str, password: str):
        user = db.query(User).filter(User.name == name).first()
        if not user:
            return None
        if not verify_password(password, str(user.hashed_password)):
            return None
        return user

    @staticmethod
    @db_query
    def get_by_name(db: Session, name: str):
        return db.query(User).filter(User.name == name).first()

    @db_update
    def delete_by_name(self, db: Session, name: str):
        user = self.get_by_name(db, name)
        if user:
            user.delete(db, user.id)
        return True
