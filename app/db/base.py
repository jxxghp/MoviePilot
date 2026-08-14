"""
ORM 基类与数据访问基类。

Base 提供声明式基类与通用的行为（字典转换、增删改查便利方法）；
DbOper 是各业务 Oper 的基类，持有一个可注入的会话。
"""
from typing import Any, List, Self, Union

from sqlalchemy import Column, Identity, Integer, Sequence, and_, delete, inspect, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, as_declarative, declared_attr

from app.runtime.config import settings
from app.db.decorators import async_db_query, async_db_update, db_query, db_update

def get_id_column():
    """
    根据数据库类型返回合适的ID列定义
    """
    if settings.DB_TYPE.lower() == "postgresql":
        # PostgreSQL使用SERIAL类型，让数据库自动处理序列
        return Column(Integer, Identity(start=1, cycle=True), primary_key=True)
    else:
        # SQLite使用Sequence
        return Column(Integer, Sequence('id'), primary_key=True)


@as_declarative()
class Base:
    id: Any
    __name__: str

    @db_update
    def create(self, db: Session):
        db.add(self)

    @async_db_update
    async def async_create(self, db: AsyncSession):
        db.add(self)
        await db.flush()
        return self

    @classmethod
    @db_query
    def get(cls, db: Session, rid: int) -> Self:
        return db.query(cls).filter(and_(cls.id == rid)).first()

    @classmethod
    @async_db_query
    async def async_get(cls, db: AsyncSession, rid: int) -> Self:
        result = await db.execute(select(cls).where(and_(cls.id == rid)))
        return result.scalars().first()

    @db_update
    def update(self, db: Session, payload: dict):
        for key, value in payload.items():
            setattr(self, key, value)
        if inspect(self).detached:
            db.add(self)

    @async_db_update
    async def async_update(self, db: AsyncSession, payload: dict):
        for key, value in payload.items():
            setattr(self, key, value)
        if inspect(self).detached:
            db.add(self)

    @classmethod
    @db_update
    def delete(cls, db: Session, rid):
        db.query(cls).filter(and_(cls.id == rid)).delete()

    @classmethod
    @async_db_update
    async def async_delete(cls, db: AsyncSession, rid):
        result = await db.execute(select(cls).where(and_(cls.id == rid)))
        user = result.scalars().first()
        if user:
            await db.delete(user)

    @classmethod
    @db_update
    def truncate(cls, db: Session):
        db.query(cls).delete()

    @classmethod
    @async_db_update
    async def async_truncate(cls, db: AsyncSession):
        await db.execute(delete(cls))

    @classmethod
    @db_query
    def list(cls, db: Session) -> List[Self]:
        return db.query(cls).all()

    @classmethod
    @async_db_query
    async def async_list(cls, db: AsyncSession) -> Sequence[Self]:
        result = await db.execute(select(cls))
        return result.scalars().all()

    def to_dict(self):
        return {c.name: getattr(self, c.name, None) for c in self.__table__.columns}  # noqa

    @declared_attr
    def __tablename__(self) -> str:
        return self.__name__.lower()


class DbOper:
    """
    数据库操作基类
    """

    def __init__(self, db: Union[Session, AsyncSession] = None):
        self._db = db
