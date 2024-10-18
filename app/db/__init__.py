from typing import Any, Self, List, Tuple, Optional, Generator

from sqlalchemy import create_engine, QueuePool, and_, inspect, MetaData
from sqlalchemy.orm import declared_attr, sessionmaker, Session, scoped_session, as_declarative

from app.core.config import settings

# 数据库引擎
Engine = create_engine(
    url=f"sqlite:///{settings.CONFIG_PATH}/user.db",
    pool_pre_ping=settings.DB_POOL_PRE_PING,
    echo=settings.DB_ECHO,
    poolclass=QueuePool,
    pool_size=settings.DB_POOL_SIZE,
    pool_recycle=settings.DB_POOL_RECYCLE,
    pool_timeout=settings.DB_POOL_TIMEOUT,
    max_overflow=settings.DB_MAX_OVERFLOW,
    connect_args={
        "timeout": settings.DB_TIMEOUT
    }
)

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


def close_database():
    """
    关闭所有数据库连接
    """
    Engine.dispose()


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

    # 数据库元数据
    metadata = MetaData()

    @db_update
    def create(self, db: Session):
        db.add(self)
        return True

    @classmethod
    @db_query
    def get(cls, db: Session, rid: int) -> Self:
        return db.query(cls).filter(cls.id == rid).first()

    @db_update
    def update(self, db: Session, payload: dict):
        payload = {k: v for k, v in payload.items() if v is not None}
        for key, value in payload.items():
            setattr(self, key, value)
        if inspect(self).detached:
            db.add(self)
        return True

    @classmethod
    @db_update
    def delete(cls, db: Session, rid):
        db.query(cls).filter(and_(cls.id == rid)).delete()
        return True

    @classmethod
    @db_update
    def truncate(cls, db: Session):
        db.query(cls).delete()
        return True

    @classmethod
    @db_query
    def list(cls, db: Session) -> List[Self]:
        result = db.query(cls).all()
        return list(result)

    def to_dict(self):
        return {c.name: getattr(self, c.name, None) for c in self.__table__.columns}

    @declared_attr
    def __tablename__(self) -> str:
        return self.__name__.lower()

    @db_update
    def relevancy_update(self, db: Session, payload: dict, relation_info: dict):
        """
        联动更新
        :param db: 数据库会话
        :param payload: 更新数据
        :param relation_info: 关联更新数据

        payload 是直接更新的数据，relation 是更新结束后，需要关联更新的数据，格式如下：
        relation_info = {
            "include_tables": ["siteuserdata", "siteicon", "sitestatistic"],  # 表名，留空则扫描所有表
            "exclude_tables": ["alembic_version" "user", "site"],  # 不需要扫描的表，留空则扫描所有表
            "column": {
                "name": {  # 字段名
                    "old_value": "old_username",  # 旧值
                    "new_value": "new_username",  # 新值
                    "include_tables": ["siteuserdata", "siteicon", "sitestatistic"],  # 指定只更新某些表，留空则更新所有表，受限于上面的 include_tables
                    "exclude_tables": ["alembic_version", "user", "site"],  # 指定某个表的这个字段不需要更新，留空则更新所有表，受限于上面的 exclude_tables
                },
                "id": {
                    "old_value": "1",
                    "new_value": "2",
                    "include_tables": ["siteuserdata", "siteicon", "sitestatistic"],  # 指定只更新某些表，留空则更新所有表
                    "exclude_tables": ["alembic_version", "user", "site"],  # 指定某个表的这个字段不需要更新，留空则更新所有表
                },
            }
        }
        """
        if not payload or not relation_info:
            raise Exception("接收的参数不全")

        # 常规更新
        payload = {k: v for k, v in payload.items() if v is not None}
        for key, value in payload.items():
            setattr(self, key, value)
        if inspect(self).detached:
            db.add(self)

        # 联动更新其他表单
        tables = Base.metadata.tables
        # 需要扫描的表
        include_tables = relation_info.get("include_tables", [])
        # 不需要扫描的表
        exclude_tables = relation_info.get("exclude_tables", [])
        for table_name, table in tables.items():
            # 一级筛选
            if ((include_tables and table_name not in include_tables)
                    or (exclude_tables and table_name in exclude_tables)):
                continue

            # 提取field中的字段
            for column, item in relation_info.get("column", {}).items():
                # 二级筛选
                in_tables = item.get("tables", [])
                not_in_tables = item.get("exclude_tables", [])
                old_value = item.get("old_value")
                new_value = item.get("new_value")
                # 不在字段指定表需要更新的表中
                if (in_tables and table_name not in in_tables) or (not_in_tables and table_name in not_in_tables):
                    continue
                # 更新字段
                if column in [table_column.name for table_column in table.columns]:
                    update_statement = table.update().where(table.c[column] == old_value).values(**{column: new_value})
                    db.execute(update_statement)
        return True


class DbOper:
    """
    数据库操作基类
    """
    _db: Session = None

    def __init__(self, db: Session = None):
        self._db = db
