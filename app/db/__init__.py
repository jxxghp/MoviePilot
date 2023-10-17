import threading

from sqlalchemy import create_engine, QueuePool
from sqlalchemy.orm import sessionmaker, Session, scoped_session

from app.core.config import settings

# 数据库引擎
Engine = create_engine(f"sqlite:///{settings.CONFIG_PATH}/user.db",
                       pool_pre_ping=True,
                       echo=False,
                       poolclass=QueuePool,
                       pool_size=1024,
                       pool_recycle=3600,
                       pool_timeout=180,
                       max_overflow=10,
                       connect_args={"timeout": 60})
# 会话工厂
SessionFactory = sessionmaker(autocommit=False, autoflush=False, bind=Engine)

# 多线程全局使用的数据库会话
ScopedSession = scoped_session(SessionFactory)

# 数据库锁
DBLock = threading.Lock()


def get_db():
    """
    获取数据库会话
    :return: Session
    """
    db = None
    try:
        db = SessionFactory()
        yield db
    finally:
        if db:
            db.close()


def db_lock(func):
    """
    使用DBLock加锁，防止多线程同时操作数据库
    装饰器
    """
    def wrapper(*args, **kwargs):
        with DBLock:
            return func(*args, **kwargs)

    return wrapper


class DbOper:
    """
    数据库操作基类
    """
    _db: Session = None

    def __init__(self, db: Session = None):
        if db:
            self._db = db
        else:
            self._db = ScopedSession()
