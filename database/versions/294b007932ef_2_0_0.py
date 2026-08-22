"""2.0.0

Revision ID: 294b007932ef
Revises:
Create Date: 2024-07-20 08:43:40.741251

"""

import secrets

from alembic import op
import sqlalchemy as sa
from app.runtime.config import settings
from app.application.security.token import get_password_hash
from app.runtime.log import logger

# revision identifiers, used by Alembic.
revision = '294b007932ef'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    v2.0.0 数据库初始化
    """
    connection = op.get_bind()
    user = sa.table(
        "user",
        sa.column("name", sa.String()),
        sa.column("email", sa.String()),
        sa.column("hashed_password", sa.String()),
        sa.column("is_active", sa.Boolean()),
        sa.column("is_superuser", sa.Boolean()),
        sa.column("avatar", sa.String()),
        sa.column("is_otp", sa.Boolean()),
        sa.column("otp_secret", sa.String()),
        sa.column("permissions", sa.JSON()),
        sa.column("settings", sa.JSON()),
    )
    # 初始化超级管理员
    existing_user = connection.execute(
        sa.select(user.c.name).where(user.c.name == settings.SUPERUSER)
    ).first()
    if not existing_user:
        if settings.SUPERUSER_PASSWORD:
            init_password = settings.SUPERUSER_PASSWORD
        else:
            # 生成随机密码
            init_password = secrets.token_urlsafe(16)
            logger.info(
                f"【超级管理员初始密码】{init_password} 请登录系统后在设定中修改。 注：该密码只会显示一次，请注意保存。")
        connection.execute(
            user.insert().values(
                name=settings.SUPERUSER,
                hashed_password=get_password_hash(init_password),
                email="admin@movie-pilot.org",
                is_active=True,
                is_superuser=True,
                avatar="",
                is_otp=False,
                otp_secret=None,
                permissions={},
                settings={},
            )
        )

    # 初始化本地存储
    systemconfig = sa.table(
        "systemconfig",
        sa.column("key", sa.String()),
        sa.column("value", sa.JSON()),
    )
    key = "Storages"
    row = connection.execute(
        sa.select(systemconfig.c.value).where(systemconfig.c.key == key)
    ).first()
    if not row or not row[0]:
        value = [
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
        ]
        if row:
            connection.execute(
                systemconfig.update().where(systemconfig.c.key == key).values(
                    value=value
                )
            )
        else:
            connection.execute(systemconfig.insert().values(key=key, value=value))


def downgrade() -> None:
    pass
