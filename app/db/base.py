"""
ORM 基类与数据访问基类。

Base 提供声明式基类与通用的行为（字典转换、增删改查便利方法）；
DbOper 是各业务 Oper 的基类，持有一个可注入的会话。
"""
from typing import Any, List, Optional, Self, Union, cast

from sqlalchemy import (CursorResult, Executable, Identity, Integer, Sequence,
                        and_, delete, inspect, select)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, declared_attr, mapped_column

from app.runtime.config import settings
from app.db.decorators import async_db_query, async_db_update, db_query, db_update


def execute_dml(db: Session, statement: Executable,
                execution_options: Optional[dict] = None) -> int:
    """
    执行 DML 语句并返回影响行数。

    ``Session.execute`` 的类型标注一律是 ``Result``，只有运行期真正拿到的
    ``CursorResult`` 才带 ``rowcount``——2.0 只为 ``Connection.execute`` 加了
    ``CursorResult`` 重载。这里把转换收口一次，免得每个模型各写一遍 cast。
    :param db: 数据库会话
    :param statement: delete()/update() 等 DML 语句
    :param execution_options: 执行选项；不传即沿用 SQLAlchemy 默认的会话同步策略
    :return: 影响行数
    """
    if execution_options is None:
        result = db.execute(statement)
    else:
        result = db.execute(statement, execution_options=execution_options)
    return cast(CursorResult[Any], result).rowcount


def get_id_column() -> Mapped[int]:
    """
    根据数据库类型返回合适的ID列定义
    """
    if settings.DB_TYPE.lower() == "postgresql":
        # PostgreSQL使用SERIAL类型，让数据库自动处理序列
        return mapped_column(Integer, Identity(start=1, cycle=True), primary_key=True)
    else:
        # SQLite使用Sequence
        return mapped_column(Integer, Sequence('id'), primary_key=True)


class Base(DeclarativeBase):
    """
    声明式基类。

    2.0 的声明式系统会解释类级 PEP 484 注解，未包裹在 Mapped[] 中的注解会直接报错。

    仓内模型已全部迁移到 mapped_column() + Mapped[] 注解，本仓自身不再需要
    __allow_unmapped__。保留它纯粹是为了兼容仓外插件：插件可以继承本 Base 自定义模型，
    其中不乏仍在用 legacy 注解风格的，关掉这个标志会让它们在 import 期就直接炸掉，
    表现为插件加载失败而非可修复的运行期报错。
    """

    __allow_unmapped__ = True

    # 由 get_id_column() 在各模型中提供实际的列定义，这里只声明类型供 IDE 使用
    id: Mapped[int]

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
    def get(cls, db: Session, rid: int) -> Optional[Self]:
        return db.execute(select(cls).where(and_(cls.id == rid))).scalars().first()

    @classmethod
    @async_db_query
    async def async_get(cls, db: AsyncSession, rid: int) -> Optional[Self]:
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
        db.execute(delete(cls).where(and_(cls.id == rid)))

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
        db.execute(delete(cls))

    @classmethod
    @async_db_update
    async def async_truncate(cls, db: AsyncSession):
        await db.execute(delete(cls))

    @classmethod
    @db_query
    def list(cls, db: Session) -> List[Self]:
        return list(db.execute(select(cls)).scalars().all())

    @classmethod
    @async_db_query
    async def async_list(cls, db: AsyncSession) -> List[Self]:
        result = await db.execute(select(cls))
        return list(result.scalars().all())

    def to_dict(self):
        return {c.name: getattr(self, c.name, None) for c in self.__table__.columns}  # noqa

    @declared_attr.directive
    def __tablename__(cls) -> str:  # noqa: N805  declared_attr 的第一个参数即类本身
        return cls.__name__.lower()


class DbOper:
    """
    数据库操作基类
    """

    def __init__(self, db: Optional[Union[Session, AsyncSession]] = None):
        self._db = db
