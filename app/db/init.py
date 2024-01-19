import random
import string

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


def init_super_user():
    """
    初始化超级管理员
    """
    # 初始化超级管理员
    with SessionFactory() as db:
        _user = User.get_by_name(db=db, name=settings.SUPERUSER)
        if not _user:
            # 定义包含数字、大小写字母的字符集合
            characters = string.ascii_letters + string.digits
            # 生成随机密码
            random_password = ''.join(random.choice(characters) for _ in range(16))
            logger.info(f"【超级管理员初始密码】{random_password} 请登录系统后在设定中修改。 注：该密码只会显示一次，请注意保存。")
            _user = User(
                name=settings.SUPERUSER,
                hashed_password=get_password_hash(random_password),
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
