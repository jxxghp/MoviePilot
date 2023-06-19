from sqlalchemy import Boolean, Column, Integer, String, Sequence
from sqlalchemy.orm import Session

from app.core.security import verify_password
from app.db.models import Base


class User(Base):
    """
    用户表
    """
    id = Column(Integer, Sequence('id'), primary_key=True, index=True)
    name = Column(String, index=True, nullable=False)
    email = Column(String, unique=True, index=True)
    hashed_password = Column(String, nullable=False)
    is_active = Column(Boolean(), default=True)
    is_superuser = Column(Boolean(), default=False)
    avatar = Column(String)

    @staticmethod
    def authenticate(db: Session, name: str, password: str):
        user = db.query(User).filter(User.name == name).first()
        if not user:
            return None
        if not verify_password(password, str(user.hashed_password)):
            return None
        return user

    @staticmethod
    def get_by_name(db: Session, name: str):
        return db.query(User).filter(User.name == name).first()

    @staticmethod
    def delete_by_name(db: Session, name: str):
        return db.query(User).filter(User.name == name).delete()
