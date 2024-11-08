from alembic.command import upgrade
from alembic.config import Config

from app.core.config import settings
from app.db import Engine, Base
from app.log import logger


def init_db():
    """
    初始化数据库
    """
    # 全量建表
    Base.metadata.create_all(bind=Engine)


def update_db():
    """
    更新数据库
    """

    script_location = settings.ROOT_PATH / 'database'
    try:
        alembic_cfg = Config()
        alembic_cfg.set_main_option('script_location', str(script_location))
        db_location = settings.CONFIG_PATH / 'user.db'
        alembic_cfg.set_main_option('sqlalchemy.url', f"sqlite:///{db_location}")
        if settings.DB_TYPE.lower() == "mysql":
            alembic_cfg.set_main_option('sqlalchemy.url', f"mysql+pymysql://{settings.DB_USER}:{settings.DB_PASSWORD}@{settings.DB_HOST}:{settings.DB_PORT}/{settings.DB_NAME}")
        upgrade(alembic_cfg, 'head')
    except Exception as e:
        logger.error(f'数据库更新失败：{str(e)}')
