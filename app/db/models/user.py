from sqlalchemy import Boolean, Column, Integer, String, Sequence
from sqlalchemy.orm import Session

from app.core.security import verify_password
from app.db.models import Base


class User(Base):
    """
    用户表
    """
    id = Column(Integer, Sequence('id'), primary_key=True, index=True)
    full_name = Column(String, index=True)
    email = Column(String, unique=True, index=True, nullable=False)
    hashed_password = Column(String, nullable=False)
    is_active = Column(Boolean(), default=True)
    is_superuser = Column(Boolean(), default=False)

    @staticmethod
    def authenticate(db: Session, email: str, password: str):
        user = db.query(User).filter(User.email == email).first()
        if not user:
            return None
        if not verify_password(password, str(user.hashed_password)):
            return None
        return user

    @staticmethod
    def get_by_email(db: Session, email: str):
        return db.query(User).filter(User.email == email).first()
