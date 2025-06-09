from typing import Any, Generator, List, Optional, Self, Tuple

from sqlalchemy import NullPool, QueuePool, and_, create_engine, inspect, text
from sqlalchemy.orm import Session, as_declarative, declared_attr, scoped_session, sessionmaker

from app.core.config import settings

# 根据池类型设置 poolclass 和相关参数
pool_class = NullPool if settings.DB_POOL_TYPE == "NullPool" else QueuePool
connect_args = {
    "timeout": settings.DB_TIMEOUT
}
# 启用 WAL 模式时的额外配置
if settings.DB_WAL_ENABLE:
    connect_args["check_same_thread"] = False
db_kwargs = {
    "url": f"sqlite:///{settings.CONFIG_PATH}/user.db",
    "pool_pre_ping": settings.DB_POOL_PRE_PING,
    "echo": settings.DB_ECHO,
    "poolclass": pool_class,
    "pool_recycle": settings.DB_POOL_RECYCLE,
    "connect_args": connect_args
}
# 当使用 QueuePool 时，添加 QueuePool 特有的参数
if pool_class == QueuePool:
    db_kwargs.update({
        "pool_size": settings.DB_POOL_SIZE,
        "pool_timeout": settings.DB_POOL_TIMEOUT,
        "max_overflow": settings.DB_MAX_OVERFLOW
    })
# 创建数据库引擎
Engine = create_engine(**db_kwargs)
# 根据配置设置日志模式
journal_mode = "WAL" if settings.DB_WAL_ENABLE else "DELETE"
with Engine.connect() as connection:
    current_mode = connection.execute(text(f"PRAGMA journal_mode={journal_mode};")).scalar()
    print(f"Database journal mode set to: {current_mode}")

# 会话工厂
SessionFactory = sessionmaker(bind=Engine)

# 多线程全局使用的数据库会话
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


def perform_checkpoint(mode: str = "PASSIVE"):
    """
    执行 SQLite 的 checkpoint 操作，将 WAL 文件内容写回主数据库
    :param mode: checkpoint 模式，可选值包括 "PASSIVE"、"FULL"、"RESTART"、"TRUNCATE"
                 默认为 "PASSIVE"，即不锁定 WAL 文件的轻量级同步
    """
    if not settings.DB_WAL_ENABLE:
        return
    valid_modes = {"PASSIVE", "FULL", "RESTART", "TRUNCATE"}
    if mode.upper() not in valid_modes:
        raise ValueError(f"Invalid checkpoint mode '{mode}'. Must be one of {valid_modes}")
    try:
        # 使用指定的 checkpoint 模式，确保 WAL 文件数据被正确写回主数据库
        with Engine.connect() as conn:
            conn.execute(text(f"PRAGMA wal_checkpoint({mode.upper()});"))
    except Exception as e:
        print(f"Error during WAL checkpoint: {e}")


def close_database():
    """
    关闭所有数据库连接并清理资源
    """
    try:
        # 释放连接池，SQLite 会自动清空 WAL 文件，这里不单独再调用 checkpoint
        Engine.dispose()
    except Exception as e:
        print(f"Error while disposing database connections: {e}")


def get_args_db(args: tuple, kwargs: dict) -> Optional[Session]:
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


def update_args_db(args: tuple, kwargs: dict, db: Session) -> Tuple[tuple, dict]:
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


def db_update(func):
    """
    数据库更新类操作装饰器，第一个参数必须是数据库会话或存在db参数
    """

    def wrapper(*args, **kwargs):
        # 是否关闭数据库会话
        _close_db = False
        # 从参数中获取数据库会话
        db = get_args_db(args, kwargs)
        if not db:
            # 如果没有获取到数据库会话，创建一个
            db = ScopedSession()
            # 标记需要关闭数据库会话
            _close_db = True
            # 更新参数中的数据库会话
            args, kwargs = update_args_db(args, kwargs, db)
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


def db_query(func):
    """
    数据库查询操作装饰器，第一个参数必须是数据库会话或存在db参数
    注意：db.query列表数据时，需要转换为list返回
    """

    def wrapper(*args, **kwargs):
        # 是否关闭数据库会话
        _close_db = False
        # 从参数中获取数据库会话
        db = get_args_db(args, kwargs)
        if not db:
            # 如果没有获取到数据库会话，创建一个
            db = ScopedSession()
            # 标记需要关闭数据库会话
            _close_db = True
            # 更新参数中的数据库会话
            args, kwargs = update_args_db(args, kwargs, db)
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


@as_declarative()
class Base:
    id: Any
    __name__: str

    @db_update
    def create(self, db: Session):
        db.add(self)

    @classmethod
    @db_query
    def get(cls, db: Session, rid: int) -> Self:
        return db.query(cls).filter(and_(cls.id == rid)).first()

    @db_update
    def update(self, db: Session, payload: dict):
        payload = {k: v for k, v in payload.items() if v is not None}
        for key, value in payload.items():
            setattr(self, key, value)
        if inspect(self).detached:
            db.add(self)

    @classmethod
    @db_update
    def delete(cls, db: Session, rid):
        db.query(cls).filter(and_(cls.id == rid)).delete()

    @classmethod
    @db_update
    def truncate(cls, db: Session):
        db.query(cls).delete()

    @classmethod
    @db_query
    def list(cls, db: Session) -> List[Self]:
        result = db.query(cls).all()
        return list(result)

    def to_dict(self):
        return {c.name: getattr(self, c.name, None) for c in self.__table__.columns} # noqa

    @declared_attr
    def __tablename__(self) -> str:
        return self.__name__.lower()


class DbOper:
    """
    数据库操作基类
    """

    def __init__(self, db: Session = None):
        self._db = db
