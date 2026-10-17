"""
2.0.0 初始化默认存储配置。

版本标识： 294b007932ef
前置版本：
创建时间： 2024-07-20 08:43:40.741251
"""

from alembic import op
import sqlalchemy as sa

# Alembic 使用以下标识连接迁移版本链。
revision = '294b007932ef'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    v2.0.0 数据库初始化
    """
    connection = op.get_bind()
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
    """
    本版本不执行回退操作，保留升级后的结构与数据。
    """
    pass
