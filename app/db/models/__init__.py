from typing import Any

from sqlalchemy.orm import as_declarative, declared_attr


@as_declarative()
class Base:
    id: Any
    __name__: str

    def create(self, db):
        db.add(self)
        db.commit()
        return self

    @classmethod
    def get(cls, db, rid: int):
        return db.query(cls).filter(cls.id == rid).first()

    def update(self, db, payload: dict):
        payload = {k: v for k, v in payload.items() if v is not None}
        for key, value in payload.items():
            setattr(self, key, value)
        db.commit()

    @classmethod
    def delete(cls, db, rid):
        db.query(cls).filter(cls.id == rid).delete()
        db.commit()

    @classmethod
    def truncate(cls, db):
        db.query(cls).delete()
        db.commit()

    @classmethod
    def list(cls, db):
        return db.query(cls).all()

    def to_dict(self):
        return {c.name: getattr(self, c.name, None) for c in self.__table__.columns}

    @declared_attr
    def __tablename__(self) -> str:
        return self.__name__.lower()
