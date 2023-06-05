from alembic.command import upgrade
from alembic.config import Config

from app.core.config import settings
from app.core.security import get_password_hash
from app.db import Engine, SessionLocal
from app.db.models import Base
from app.db.models.user import User
from app.log import logger


def init_db():
    """
    初始化数据库
    """
    Base.metadata.create_all(bind=Engine)
    # 初始化超级管理员
    _db = SessionLocal()
    user = User.get_by_email(db=_db, email=settings.SUPERUSER)
    if not user:
        user = User(
            full_name="Admin",
            email=settings.SUPERUSER,
            hashed_password=get_password_hash(settings.SUPERUSER_PASSWORD),
            is_superuser=True,
        )
        user.create(_db)


def update_db():
    """
    更新数据库
    """
    db_location = settings.CONFIG_PATH / 'user.db'
    script_location = settings.ROOT_PATH / 'alembic'
    try:
        alembic_cfg = Config()
        alembic_cfg.set_main_option('script_location', str(script_location))
        alembic_cfg.set_main_option('sqlalchemy.url', f"sqlite:///{db_location}")
        upgrade(alembic_cfg, 'head')
    except Exception as e:
        logger(f'数据库更新失败：{e}')
