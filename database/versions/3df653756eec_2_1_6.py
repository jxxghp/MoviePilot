"""
2.1.6 为尚无权限配置的普通用户补齐发现、搜索和订阅权限。

版本标识： 3df653756eec
前置版本： 486e56a62dcb
创建时间： 2025-06-11 19:52:57.185355
"""
from alembic import op
import sqlalchemy as sa

# Alembic 使用以下标识连接迁移版本链。
revision = '3df653756eec'
down_revision = '486e56a62dcb'
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    为尚无权限配置的普通用户补齐发现、搜索和订阅权限。
    """
    connection = op.get_bind()
    user = sa.table(
        "user",
        sa.column("id", sa.Integer()),
        sa.column("is_superuser", sa.Boolean()),
        sa.column("permissions", sa.JSON()),
    )
    users = connection.execute(
        sa.select(user.c.id, user.c.is_superuser, user.c.permissions)
    ).mappings().all()
    permissions = {
        "discovery": True,
        "search": True,
        "subscribe": True,
        "manage": False,
    }
    for item in users:
        if item["is_superuser"] or item["permissions"]:
            continue
        connection.execute(
            user.update()
            .where(user.c.id == item["id"])
            .values(permissions=permissions)
        )


def downgrade() -> None:
    """
    本版本不执行回退操作，保留升级后的结构与数据。
    """
    pass
