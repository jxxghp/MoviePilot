from typing import Any, Self, List

from sqlalchemy.orm import as_declarative, declared_attr, Session


@as_declarative()
class Base:
    id: Any
    __name__: str

    @staticmethod
    def commit(db: Session):
        try:
            db.commit()
        except Exception as err:
            db.rollback()
            raise err

    def create(self, db: Session) -> Self:
        db.add(self)
        self.commit(db)
        return self

    @classmethod
    def get(cls, db: Session, rid: int) -> Self:
        return db.query(cls).filter(cls.id == rid).first()

    def update(self, db: Session, payload: dict):
        payload = {k: v for k, v in payload.items() if v is not None}
        for key, value in payload.items():
            setattr(self, key, value)
        Base.commit(db)

    @classmethod
    def delete(cls, db: Session, rid):
        db.query(cls).filter(cls.id == rid).delete()
        Base.commit(db)

    @classmethod
    def truncate(cls, db: Session):
        db.query(cls).delete()
        Base.commit(db)

    @classmethod
    def list(cls, db: Session) -> List[Self]:
        return db.query(cls).all()

    def to_dict(self):
        return {c.name: getattr(self, c.name, None) for c in self.__table__.columns}

    @declared_attr
    def __tablename__(self) -> str:
        return self.__name__.lower()
