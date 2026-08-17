from configparser import ConfigParser as _ConfigParser
import traceback

from alembic.command import upgrade
from alembic.config import Config

from app.runtime.config import settings
from app.db import Base
from app.db.models import load_all_models
from app.runtime.log import logger


def init_db():
    """
    初始化数据库
    """
    # 函数内导入而非模块级：写成模块级会让 import 本模块的一方也被迫拉起引擎模块。
    # 引擎一律用 get_engine() 取——旧名字 `app.db.Engine` 只为仓库外插件保留，且它一经
    # 属性访问就把引擎建出来，模块级写法会使本模块反过来依赖「数据库已在别处初始化完成」。
    from app.db.engine import get_engine

    # 确保所有模型都已注册到 Base.metadata 中
    load_all_models()

    # 全量建表
    Base.metadata.create_all(bind=get_engine())


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
