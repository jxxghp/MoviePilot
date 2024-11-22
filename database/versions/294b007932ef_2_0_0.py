"""2.0.0

Revision ID: 294b007932ef
Revises: 
Create Date: 2024-07-20 08:43:40.741251

"""

import secrets

from app.core.config import settings
from app.core.security import get_password_hash
from app.db import SessionFactory
from app.db.models import *
from app.db.systemconfig_oper import SystemConfigOper
from app.log import logger
from app.schemas.types import SystemConfigKey

# revision identifiers, used by Alembic.
revision = '294b007932ef'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    v2.0.0 数据库初始化
    """
    with SessionFactory() as db:
        # 初始化超级管理员
        _user = User.get_by_name(db=db, name=settings.SUPERUSER)
        if not _user:
            # 生成随机密码
            random_password = secrets.token_urlsafe(16)
            logger.info(
                f"【超级管理员初始密码】{random_password} 请登录系统后在设定中修改。 注：该密码只会显示一次，请注意保存。")
            _user = User(
                name=settings.SUPERUSER,
                hashed_password=get_password_hash(random_password),
                email="admin@movie-pilot.org",
                is_superuser=True,
                avatar=""
            )
            _user.create(db)
        # 初始化本地存储
        _systemconfig = SystemConfigOper()
        if not _systemconfig.get(SystemConfigKey.Storages):
            _systemconfig.set(SystemConfigKey.Storages, [
                {
                    "type": "local",
                    "name": "本地",
                    "config": {}
                },
                {
                    "type": "alipan",
                    "name": "阿里云盘",
                    "config": {}
                },
                {
                    "type": "u115",
                    "name": "115网盘",
                    "config": {}
                },
                {
                    "type": "rclone",
                    "name": "RClone",
                    "config": {}
                }
            ])


def downgrade() -> None:
    pass
