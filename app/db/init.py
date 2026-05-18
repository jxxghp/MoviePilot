from configparser import ConfigParser as _ConfigParser

from alembic.command import upgrade
from alembic.config import Config

from app.core.config import settings
from app.db import Engine, Base
from app.log import logger


def init_db():
    """
    初始化数据库
    """
    # 确保所有模型都已注册到 Base.metadata 中
    import app.db.models  # noqa: F401

    # 全量建表
    Base.metadata.create_all(bind=Engine) # noqa


def update_db():
    """
    更新数据库
    """
    script_location = settings.ROOT_PATH / 'database'
    try:
        alembic_cfg = Config()
        alembic_cfg.file_config = _ConfigParser(interpolation=None)
        alembic_cfg.set_main_option('script_location', str(script_location))
        
        # 根据数据库类型设置不同的URL
        if settings.DB_TYPE.lower() == "postgresql":
            db_url = settings.DB_POSTGRESQL_URL()
        else:
            db_location = settings.CONFIG_PATH / 'user.db'
            db_url = f"sqlite:///{db_location}"
            
        alembic_cfg.set_main_option('sqlalchemy.url', db_url)
        upgrade(alembic_cfg, 'head')
    except Exception as e:
        logger.error(f'数据库更新失败：{str(e)}')
