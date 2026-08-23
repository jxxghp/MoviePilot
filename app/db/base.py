"""
ORM 基类与数据访问基类。

Base 提供声明式基类与显式会话增删改查原语；
DbOper 是各业务 Oper 的基类，持有一个可注入的会话。
"""
from collections.abc import Awaitable, Callable
from typing import Any, List, Optional, Self, TypeVar, Union, cast

from sqlalchemy import (CursorResult, Executable, Identity, Integer, Sequence,
                        and_, delete, inspect, select)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, declared_attr, mapped_column

from app.db.uow import run_async_transaction, run_sync_transaction
from app.runtime.config import settings


T = TypeVar("T")


def execute_dml(db: Session, statement: Executable,
                execution_options: Optional[dict[str, Any]] = None) -> int:
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
    return int(cast(CursorResult[Any], result).rowcount)


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


class Base(DeclarativeBase):  # type: ignore[misc]  # SQLAlchemy 无 py.typed 基类
    """
    声明式基类。

    2.0 的声明式系统会解释类级 PEP 484 注解，未包裹在 Mapped[] 中的注解会直接报错。
    仓内模型已全部迁移到 mapped_column() + Mapped[] 注解，因此不设 __allow_unmapped__：
    该标志此前只为「仓外插件可能继承本 Base 自定义 legacy 注解模型」保留，插件生态
    确定迭代后这条理由不再成立。留着它反而会让回流的 1.x 写法在 import 期悄悄通过，
    等到运行期才以「列不存在」的形式暴露。

    继承本类的模型一律使用 mapped_column() + Mapped[] 注解；确需非映射的类级属性时
    用 ClassVar 显式声明，而不是把这个标志加回来。

    create/get/update/delete/list/truncate 及其异步版本都是显式会话原语：只在调用方
    Session 中暂存或查询，不自行创建、提交、回滚或关闭事务。宿主业务代码应通过 Oper
    或 Application Command 使用这些能力，插件不得直接依赖宿主模型。
    """

    # 由 get_id_column() 在各模型中提供实际的列定义，这里只声明类型供 IDE 使用
    id: Mapped[int]

    def create(self, db: Session) -> None:
        """在调用方同步事务中暂存当前模型。"""
        db.add(self)

    async def async_create(self, db: AsyncSession) -> Self:
        """在调用方异步事务中暂存当前模型并刷新主键。"""
        db.add(self)
        await db.flush()
        return self

    @classmethod
    def get(cls, db: Session, rid: int) -> Optional[Self]:
        """在调用方同步会话中按主键查询当前模型。"""
        return cast(
            Optional[Self],
            db.execute(select(cls).where(and_(cls.id == rid))).scalars().first(),
        )

    @classmethod
    async def async_get(cls, db: AsyncSession, rid: int) -> Optional[Self]:
        """在调用方异步会话中按主键查询当前模型。"""
        result = await db.execute(select(cls).where(and_(cls.id == rid)))
        return cast(Optional[Self], result.scalars().first())

    def update(self, db: Session, payload: dict[str, Any]) -> None:
        """在调用方同步事务中更新当前模型字段。"""
        for key, value in payload.items():
            setattr(self, key, value)
        if inspect(self).detached:
            db.add(self)

    async def async_update(
        self,
        db: AsyncSession,
        payload: dict[str, Any],
    ) -> None:
        """在调用方异步事务中更新当前模型字段。"""
        for key, value in payload.items():
            setattr(self, key, value)
        if inspect(self).detached:
            db.add(self)

    @classmethod
    def delete(cls, db: Session, rid: Any) -> None:
        """在调用方同步事务中按主键删除当前模型。"""
        db.execute(delete(cls).where(and_(cls.id == rid)))

    @classmethod
    async def async_delete(cls, db: AsyncSession, rid: Any) -> None:
        """在调用方异步事务中按主键删除当前模型。"""
        result = await db.execute(select(cls).where(and_(cls.id == rid)))
        user = result.scalars().first()
        if user:
            await db.delete(user)

    @classmethod
    def truncate(cls, db: Session) -> None:
        """在调用方同步事务中清空当前模型表。"""
        db.execute(delete(cls))

    @classmethod
    async def async_truncate(cls, db: AsyncSession) -> None:
        """在调用方异步事务中清空当前模型表。"""
        await db.execute(delete(cls))

    @classmethod
    def list(cls, db: Session) -> List[Self]:
        """在调用方同步会话中查询当前模型的全部记录。"""
        return list(db.execute(select(cls)).scalars().all())

    @classmethod
    async def async_list(cls, db: AsyncSession) -> List[Self]:
        """在调用方异步会话中查询当前模型的全部记录。"""
        result = await db.execute(select(cls))
        return list(result.scalars().all())

    def to_dict(self) -> dict[str, Any]:
        """把当前模型的映射列转换为字典。"""
        return {c.name: getattr(self, c.name, None) for c in self.__table__.columns}  # noqa

    @declared_attr.directive  # type: ignore[misc]  # SQLAlchemy decorator 缺少类型信息
    def __tablename__(cls) -> str:  # noqa: N805  declared_attr 的第一个参数即类本身
        return str(cls.__name__).lower()


TModel = TypeVar("TModel", bound=Base)


class DbOper:
    """
    数据库操作基类
    """

    def __init__(self, db: Optional[Union[Session, AsyncSession]] = None):
        """保存调用方会话；无会话调用由组合根事务执行器承接。"""
        self._db = db

    def _execute_sync_write(self, operation: Callable[[Session], T]) -> T:
        """在当前同步会话暂存，或委托组合根创建事务。"""
        if self._db is None or isinstance(self._db, AsyncSession):
            # 旧调用可能在同一 Oper 上混用同步/异步方法；跨会话类型时使用匹配的
            # 独立事务，不能把 AsyncSession 交给同步 SQLAlchemy API。
            return run_sync_transaction(operation)
        return operation(self._db)

    def _execute_sync_query(self, operation: Callable[[Session], T]) -> T:
        """在当前同步会话查询，或委托组合根创建一次性会话。"""
        if self._db is None or isinstance(self._db, AsyncSession):
            return run_sync_transaction(operation)
        return operation(self._db)

    async def _execute_async_write(
        self,
        operation: Callable[[AsyncSession], Awaitable[T]],
    ) -> T:
        """在当前异步会话暂存，或委托组合根创建事务。"""
        if self._db is None or isinstance(self._db, Session):
            # 同步会话不会被错误传入异步模型写入，而是由组合根另开匹配的异步事务。
            return await run_async_transaction(operation)
        return await operation(self._db)

    async def _execute_async_query(
        self,
        operation: Callable[[AsyncSession], Awaitable[T]],
    ) -> T:
        """在当前异步会话查询，或委托组合根创建一次性会话。"""
        if self._db is None or isinstance(self._db, Session):
            return await run_async_transaction(operation)
        return await operation(self._db)

    def _stage_create(self, model: TModel) -> TModel:
        """在调用方或组合根持有的同步事务中暂存新模型。"""
        def stage(session: Session) -> TModel:
            """把模型加入当前同步会话。"""
            session.add(model)
            return model

        return self._execute_sync_write(stage)

    async def _stage_async_create(self, model: TModel) -> TModel:
        """在显式异步事务中暂存新模型并刷新主键。"""
        async def stage(session: AsyncSession) -> TModel:
            """把模型加入当前异步会话并刷新。"""
            session.add(model)
            await session.flush()
            return model

        return await self._execute_async_write(stage)

    def _stage_update(self, model: TModel, payload: dict[str, Any]) -> TModel:
        """在显式同步事务中更新模型字段，必要时重新附加游离对象。"""
        def stage(session: Session) -> TModel:
            """应用字段并把游离模型重新加入会话。"""
            for key, value in payload.items():
                setattr(model, key, value)
            model_state = inspect(model, raiseerr=False)
            if model_state is not None and model_state.detached:
                session.add(model)
            return model

        return self._execute_sync_write(stage)

    async def _stage_async_update(
        self,
        model: TModel,
        payload: dict[str, Any],
    ) -> TModel:
        """在显式异步事务中更新模型字段，必要时重新附加游离对象。"""
        async def stage(session: AsyncSession) -> TModel:
            """应用字段并把游离模型重新加入会话。"""
            for key, value in payload.items():
                setattr(model, key, value)
            model_state = inspect(model, raiseerr=False)
            if model_state is not None and model_state.detached:
                session.add(model)
            return model

        return await self._execute_async_write(stage)

    def _stage_delete(self, model_type: type[Base], rid: Any) -> None:
        """在显式同步事务中按主键删除模型。"""
        self._execute_sync_write(
            lambda session: session.execute(
                delete(model_type).where(model_type.id == rid)
            )
        )

    async def _stage_async_delete(self, model_type: type[Base], rid: Any) -> None:
        """在显式异步事务中按主键删除模型。"""
        async def stage(session: AsyncSession) -> None:
            """执行当前异步事务内的按主键删除。"""
            await session.execute(delete(model_type).where(model_type.id == rid))

        await self._execute_async_write(stage)

    def _stage_truncate(self, model_type: type[Base]) -> None:
        """在显式同步事务中删除模型表的全部记录。"""
        self._execute_sync_write(
            lambda session: session.execute(delete(model_type))
        )

    async def _stage_async_truncate(self, model_type: type[Base]) -> None:
        """在显式异步事务中删除模型表的全部记录。"""
        async def stage(session: AsyncSession) -> None:
            """执行当前异步事务内的全表删除。"""
            await session.execute(delete(model_type))

        await self._execute_async_write(stage)
