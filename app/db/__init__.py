import asyncio
from typing import Any, Generator, List, Optional, Self, Tuple, AsyncGenerator, Union

from sqlalchemy import NullPool, QueuePool, and_, create_engine, inspect, text, select, delete, Column, Integer, \
    Sequence, Identity
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session, as_declarative, declared_attr, scoped_session, sessionmaker

from app.core.config import settings


def get_id_column():
    """
    根据数据库类型返回合适的ID列定义
    """
    if settings.DB_TYPE.lower() == "postgresql":
        # PostgreSQL使用SERIAL类型，让数据库自动处理序列
        return Column(Integer, Identity(start=1, cycle=True), primary_key=True, index=True)
    else:
        # SQLite使用Sequence
        return Column(Integer, Sequence('id'), primary_key=True, index=True)


def _get_database_engine(is_async: bool = False):
    """
    获取数据库连接参数并设置WAL模式
    :param is_async: 是否创建异步引擎，True - 异步引擎, False - 同步引擎
    :return: 返回对应的数据库引擎
    """
    # 根据数据库类型选择连接方式
    if settings.DB_TYPE.lower() == "postgresql":
        return _get_postgresql_engine(is_async)
    else:
        return _get_sqlite_engine(is_async)


def _get_sqlite_engine(is_async: bool = False):
    """
    获取SQLite数据库引擎
    """
    # 连接参数
    _connect_args = {
        "timeout": settings.DB_TIMEOUT,
    }
    # 启用 WAL 模式时的额外配置
    if settings.DB_WAL_ENABLE:
        _connect_args["check_same_thread"] = False

    # 创建同步引擎
    if not is_async:
        # 根据池类型设置 poolclass 和相关参数
        _pool_class = NullPool if settings.DB_POOL_TYPE == "NullPool" else QueuePool

        # 数据库参数
        _db_kwargs = {
            "url": f"sqlite:///{settings.CONFIG_PATH}/user.db",
            "pool_pre_ping": settings.DB_POOL_PRE_PING,
            "echo": settings.DB_ECHO,
            "poolclass": _pool_class,
            "pool_recycle": settings.DB_POOL_RECYCLE,
            "connect_args": _connect_args
        }

        # 当使用 QueuePool 时，添加 QueuePool 特有的参数
        if _pool_class == QueuePool:
            _db_kwargs.update({
                "pool_size": settings.DB_SQLITE_POOL_SIZE,
                "pool_timeout": settings.DB_POOL_TIMEOUT,
                "max_overflow": settings.DB_SQLITE_MAX_OVERFLOW
            })

        # 创建数据库引擎
        engine = create_engine(**_db_kwargs)

        # 设置WAL模式
        _journal_mode = "WAL" if settings.DB_WAL_ENABLE else "DELETE"
        with engine.connect() as connection:
            current_mode = connection.execute(text(f"PRAGMA journal_mode={_journal_mode};")).scalar()
            print(f"SQLite database journal mode set to: {current_mode}")

        return engine
    else:
        # 数据库参数，只能使用 NullPool
        _db_kwargs = {
            "url": f"sqlite+aiosqlite:///{settings.CONFIG_PATH}/user.db",
            "pool_pre_ping": settings.DB_POOL_PRE_PING,
            "echo": settings.DB_ECHO,
            "poolclass": NullPool,
            "pool_recycle": settings.DB_POOL_RECYCLE,
            "connect_args": _connect_args
        }
        # 创建异步数据库引擎
        async_engine = create_async_engine(**_db_kwargs)

        # 设置WAL模式
        _journal_mode = "WAL" if settings.DB_WAL_ENABLE else "DELETE"

        async def set_async_wal_mode():
            """
            设置异步引擎的WAL模式
            """
            async with async_engine.connect() as _connection:
                result = await _connection.execute(text(f"PRAGMA journal_mode={_journal_mode};"))
                _current_mode = result.scalar()
                print(f"Async SQLite database journal mode set to: {_current_mode}")

        try:
            asyncio.run(set_async_wal_mode())
        except Exception as e:
            print(f"Failed to set async SQLite WAL mode: {e}")

        return async_engine


def _get_postgresql_engine(is_async: bool = False):
    """
    获取PostgreSQL数据库引擎
    """
    # 构建PostgreSQL连接URL
    if settings.DB_POSTGRESQL_PASSWORD:
        db_url = f"postgresql://{settings.DB_POSTGRESQL_USERNAME}:{settings.DB_POSTGRESQL_PASSWORD}@{settings.DB_POSTGRESQL_HOST}:{settings.DB_POSTGRESQL_PORT}/{settings.DB_POSTGRESQL_DATABASE}"
    else:
        db_url = f"postgresql://{settings.DB_POSTGRESQL_USERNAME}@{settings.DB_POSTGRESQL_HOST}:{settings.DB_POSTGRESQL_PORT}/{settings.DB_POSTGRESQL_DATABASE}"

    # PostgreSQL连接参数
    _connect_args = {}

    # 创建同步引擎
    if not is_async:
        # 根据池类型设置 poolclass 和相关参数
        _pool_class = NullPool if settings.DB_POOL_TYPE == "NullPool" else QueuePool

        # 数据库参数
        _db_kwargs = {
            "url": db_url,
            "pool_pre_ping": settings.DB_POOL_PRE_PING,
            "echo": settings.DB_ECHO,
            "poolclass": _pool_class,
            "pool_recycle": settings.DB_POOL_RECYCLE,
            "connect_args": _connect_args
        }

        # 当使用 QueuePool 时，添加 QueuePool 特有的参数
        if _pool_class == QueuePool:
            _db_kwargs.update({
                "pool_size": settings.DB_POSTGRESQL_POOL_SIZE,
                "pool_timeout": settings.DB_POOL_TIMEOUT,
                "max_overflow": settings.DB_POSTGRESQL_MAX_OVERFLOW
            })

        # 创建数据库引擎
        engine = create_engine(**_db_kwargs)
        print(f"PostgreSQL database connected to {settings.DB_POSTGRESQL_HOST}:{settings.DB_POSTGRESQL_PORT}/{settings.DB_POSTGRESQL_DATABASE}")

        return engine
    else:
        # 构建异步PostgreSQL连接URL
        async_db_url = f"postgresql+asyncpg://{settings.DB_POSTGRESQL_USERNAME}:{settings.DB_POSTGRESQL_PASSWORD}@{settings.DB_POSTGRESQL_HOST}:{settings.DB_POSTGRESQL_PORT}/{settings.DB_POSTGRESQL_DATABASE}"

        # 数据库参数，只能使用 NullPool
        _db_kwargs = {
            "url": async_db_url,
            "pool_pre_ping": settings.DB_POOL_PRE_PING,
            "echo": settings.DB_ECHO,
            "poolclass": NullPool,
            "pool_recycle": settings.DB_POOL_RECYCLE,
            "connect_args": _connect_args
        }
        # 创建异步数据库引擎
        async_engine = create_async_engine(**_db_kwargs)
        print(f"Async PostgreSQL database connected to {settings.DB_POSTGRESQL_HOST}:{settings.DB_POSTGRESQL_PORT}/{settings.DB_POSTGRESQL_DATABASE}")

        return async_engine


# 同步数据库引擎
Engine = _get_database_engine(is_async=False)

# 异步数据库引擎
AsyncEngine = _get_database_engine(is_async=True)

# 同步会话工厂
SessionFactory = sessionmaker(bind=Engine)

# 异步会话工厂
AsyncSessionFactory = async_sessionmaker(bind=AsyncEngine, class_=AsyncSession)

# 同步多线程全局使用的数据库会话
ScopedSession = scoped_session(SessionFactory)


def get_db() -> Generator:
    """
    获取数据库会话，用于WEB请求
    :return: Session
    """
    db = None
    try:
        db = SessionFactory()
        yield db
    finally:
        if db:
            db.close()


async def get_async_db() -> AsyncGenerator[AsyncSession, None]:
    """
    获取异步数据库会话，用于WEB请求
    :return: AsyncSession
    """
    async with AsyncSessionFactory() as session:
        try:
            yield session
        finally:
            await session.close()


async def close_database():
    """
    关闭所有数据库连接并清理资源
    """
    try:
        # 释放同步连接池
        Engine.dispose()  # noqa
        # 释放异步连接池
        await AsyncEngine.dispose()
    except Exception as err:
        print(f"Error while disposing database connections: {err}")


def _get_args_db(args: tuple, kwargs: dict) -> Optional[Session]:
    """
    从参数中获取数据库Session对象
    """
    db = None
    if args:
        for arg in args:
            if isinstance(arg, Session):
                db = arg
                break
    if kwargs:
        for key, value in kwargs.items():
            if isinstance(value, Session):
                db = value
                break
    return db


def _get_args_async_db(args: tuple, kwargs: dict) -> Optional[AsyncSession]:
    """
    从参数中获取异步数据库AsyncSession对象
    """
    db = None
    if args:
        for arg in args:
            if isinstance(arg, AsyncSession):
                db = arg
                break
    if kwargs:
        for key, value in kwargs.items():
            if isinstance(value, AsyncSession):
                db = value
                break
    return db


def _update_args_db(args: tuple, kwargs: dict, db: Session) -> Tuple[tuple, dict]:
    """
    更新参数中的数据库Session对象，关键字传参时更新db的值，否则更新第1或第2个参数
    """
    if kwargs and 'db' in kwargs:
        kwargs['db'] = db
    elif args:
        if args[0] is None:
            args = (db, *args[1:])
        else:
            args = (args[0], db, *args[2:])
    return args, kwargs


def _update_args_async_db(args: tuple, kwargs: dict, db: AsyncSession) -> Tuple[tuple, dict]:
    """
    更新参数中的异步数据库AsyncSession对象，关键字传参时更新db的值，否则更新第1或第2个参数
    """
    if kwargs and 'db' in kwargs:
        kwargs['db'] = db
    elif args:
        if args[0] is None:
            args = (db, *args[1:])
        else:
            args = (args[0], db, *args[2:])
    return args, kwargs


def db_update(func):
    """
    数据库更新类操作装饰器，第一个参数必须是数据库会话或存在db参数
    """

    def wrapper(*args, **kwargs):
        # 是否关闭数据库会话
        _close_db = False
        # 从参数中获取数据库会话
        db = _get_args_db(args, kwargs)
        if not db:
            # 如果没有获取到数据库会话，创建一个
            db = ScopedSession()
            # 标记需要关闭数据库会话
            _close_db = True
            # 更新参数中的数据库会话
            args, kwargs = _update_args_db(args, kwargs, db)
        try:
            # 执行函数
            result = func(*args, **kwargs)
            # 提交事务
            db.commit()
        except Exception as err:
            # 回滚事务
            db.rollback()
            raise err
        finally:
            # 关闭数据库会话
            if _close_db:
                db.close()
        return result

    return wrapper


def async_db_update(func):
    """
    异步数据库更新类操作装饰器，第一个参数必须是异步数据库会话或存在db参数
    """

    async def wrapper(*args, **kwargs):
        # 是否关闭数据库会话
        _close_db = False
        # 从参数中获取异步数据库会话
        db = _get_args_async_db(args, kwargs)
        if not db:
            # 如果没有获取到异步数据库会话，创建一个
            db = AsyncSessionFactory()
            # 标记需要关闭数据库会话
            _close_db = True
            # 更新参数中的异步数据库会话
            args, kwargs = _update_args_async_db(args, kwargs, db)
        try:
            # 执行函数
            result = await func(*args, **kwargs)
            # 提交事务
            await db.commit()
        except Exception as err:
            # 回滚事务
            await db.rollback()
            raise err
        finally:
            # 关闭数据库会话
            if _close_db:
                await db.close()
        return result

    return wrapper


def db_query(func):
    """
    数据库查询操作装饰器，第一个参数必须是数据库会话或存在db参数
    注意：db.query列表数据时，需要转换为list返回
    """

    def wrapper(*args, **kwargs):
        # 是否关闭数据库会话
        _close_db = False
        # 从参数中获取数据库会话
        db = _get_args_db(args, kwargs)
        if not db:
            # 如果没有获取到数据库会话，创建一个
            db = ScopedSession()
            # 标记需要关闭数据库会话
            _close_db = True
            # 更新参数中的数据库会话
            args, kwargs = _update_args_db(args, kwargs, db)
        try:
            # 执行函数
            result = func(*args, **kwargs)
        except Exception as err:
            raise err
        finally:
            # 关闭数据库会话
            if _close_db:
                db.close()
        return result

    return wrapper


def async_db_query(func):
    """
    异步数据库查询操作装饰器，第一个参数必须是异步数据库会话或存在db参数
    注意：db.query列表数据时，需要转换为list返回
    """

    async def wrapper(*args, **kwargs):
        # 是否关闭数据库会话
        _close_db = False
        # 从参数中获取异步数据库会话
        db = _get_args_async_db(args, kwargs)
        if not db:
            # 如果没有获取到异步数据库会话，创建一个
            db = AsyncSessionFactory()
            # 标记需要关闭数据库会话
            _close_db = True
            # 更新参数中的异步数据库会话
            args, kwargs = _update_args_async_db(args, kwargs, db)
        try:
            # 执行函数
            result = await func(*args, **kwargs)
        except Exception as err:
            raise err
        finally:
            # 关闭数据库会话
            if _close_db:
                await db.close()
        return result

    return wrapper


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
