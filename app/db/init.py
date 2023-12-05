from alembic.command import upgrade
from alembic.config import Config

from app.core.config import settings
from app.core.security import get_password_hash
from app.db import Engine, SessionFactory, Base
from app.db.models import *
from app.log import logger


def init_db():
    """
    初始化数据库
    """
    # 全量建表
    Base.metadata.create_all(bind=Engine)
    # 初始化超级管理员
    with SessionFactory() as db:
        _user = User.get_by_name(db=db, name=settings.SUPERUSER)
        if not _user:
            _user = User(
                name=settings.SUPERUSER,
                hashed_password=get_password_hash(settings.SUPERUSER_PASSWORD),
                is_superuser=True,
            )
            _user.create(db)


def update_db():
    """
    更新数据库
    """
    db_location = settings.CONFIG_PATH / 'user.db'
    script_location = settings.ROOT_PATH / 'database'
    try:
        alembic_cfg = Config()
        alembic_cfg.set_main_option('script_location', str(script_location))
        alembic_cfg.set_main_option('sqlalchemy.url', f"sqlite:///{db_location}")
        upgrade(alembic_cfg, 'head')
    except Exception as e:
        logger.error(f'数据库更新失败：{str(e)}')
