from sqlalchemy import create_engine, QueuePool
from sqlalchemy.orm import sessionmaker, Session

from app.core.config import settings

# 数据库引擎
Engine = create_engine(f"sqlite:///{settings.CONFIG_PATH}/user.db",
                       pool_pre_ping=True,
                       echo=False,
                       poolclass=QueuePool,
                       pool_size=1000,
                       pool_recycle=60 * 10,
                       max_overflow=0)
# 数据库会话
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=Engine)


def get_db():
    """
    获取数据库会话
    :return: Session
    """
    db = None
    try:
        db = SessionLocal()
        yield db
    finally:
        if db:
            db.close()


class DbOper:

    _db: Session = None

    def __init__(self, db: Session = None):
        if db:
            self._db = db
        else:
            self._db = SessionLocal()

    def __del__(self):
        if self._db:
            self._db.close()
