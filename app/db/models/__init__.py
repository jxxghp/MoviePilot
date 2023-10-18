import threading
from typing import Any, Self, List

from sqlalchemy.orm import as_declarative, declared_attr, Session

from app.db import ScopedSession, DBLock


def db_persist(func):
    """
    数据库操作装饰器，获取第一个输入参数db，执行数据库操作后提交
    """

    def wrapper(*args, **kwargs):
        with DBLock:
            db: Session = kwargs.get("db")
            if not db:
                for arg in args:
                    if isinstance(arg, Session):
                        db = arg
                        break
            try:
                if db:
                    db.close()
                db = ScopedSession()
                result = func(*args, **kwargs)
                db.commit()
            except Exception as err:
                db.rollback()
                raise err
            return result

    return wrapper


@as_declarative()
class Base:
    id: Any
    __name__: str

    @db_persist
    def create(self, db: Session) -> Self:
        db.add(self)
        return self

    @classmethod
    def get(cls, db: Session, rid: int) -> Self:
        return db.query(cls).filter(cls.id == rid).first()

    @db_persist
    def update(self, db: Session, payload: dict):
        payload = {k: v for k, v in payload.items() if v is not None}
        for key, value in payload.items():
            setattr(self, key, value)

    @classmethod
    @db_persist
    def delete(cls, db: Session, rid):
        db.query(cls).filter(cls.id == rid).delete()

    @classmethod
    @db_persist
    def truncate(cls, db: Session):
        db.query(cls).delete()

    @classmethod
    def list(cls, db: Session) -> List[Self]:
        return db.query(cls).all()

    def to_dict(self):
        return {c.name: getattr(self, c.name, None) for c in self.__table__.columns}

    @declared_attr
    def __tablename__(self) -> str:
        return self.__name__.lower()
