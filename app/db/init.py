from configparser import ConfigParser as _ConfigParser
import traceback

from alembic.command import upgrade
from alembic.config import Config

from app.runtime.config import settings
from app.db import Base
from app.runtime.log import logger


def init_db():
    """
    初始化数据库
    """
    # 在函数内取引擎而不是模块级导入：模块级 `from app.db import Engine` 会在
    # import 本模块时就把引擎对象绑定下来，使本模块反过来依赖「数据库已在别处
    # 初始化完成」。函数内获取才能让引擎的创建时机由调用方决定。
    from app.db.engine import Engine

    # 确保所有模型都已注册到 Base.metadata 中
    import app.db.models  # noqa: F401

    # 全量建表
    Base.metadata.create_all(bind=Engine)  # noqa


def update_db():
    """
    更新数据库
    """
    script_location = settings.ROOT_PATH / 'database'
    try:
        alembic_cfg = Config()
        alembic_cfg.file_config = _ConfigParser(interpolation=None)
        alembic_cfg.set_main_option('script_location', str(script_location))
        
        # 与引擎构建使用同一套 URL 推导：两处各自拼接会在配置变更时悄悄漂移，
        # 导致迁移连到与应用不同的库上
        db_url = settings.DB_SQLITE_URL() if settings.DB_TYPE.lower() != "postgresql" \
            else settings.DB_POSTGRESQL_URL()
            
        alembic_cfg.set_main_option('sqlalchemy.url', db_url)
        upgrade(alembic_cfg, 'head')
    except Exception as error:
        logger.error(
            f'数据库更新失败：{str(error)} - {traceback.format_exc()}'
        )
        raise
