"""
2.1.7 将已配置的 AList 存储显示名称更新为 OpenList。

版本标识： 3891a5e722a1
前置版本： 3df653756eec
创建时间： 2025-06-28 08:40:14.516836
"""
from alembic import op
import sqlalchemy as sa

from sqlalchemy.dialects import sqlite


# Alembic 使用以下标识连接迁移版本链。
revision = '3891a5e722a1'
down_revision = '3df653756eec'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 保留存储类型标识，只更新用户可见名称。
    """
    将已配置的 AList 存储显示名称更新为 OpenList。
    """
    systemconfig = sa.table(
        "systemconfig",
        sa.column("key", sa.String()),
        sa.column("value", sa.JSON()),
    )
    connection = op.get_bind()
    key = "Storages"
    row = connection.execute(
        sa.select(systemconfig.c.value).where(systemconfig.c.key == key)
    ).first()
    _storages = row[0] if row else None
    if _storages:
        changed = False
        for storage in _storages:
            if storage["type"] == "alist":
                if storage.get("name") != "OpenList":
                    storage["name"] = "OpenList"
                    changed = True
                break
        if changed:
            connection.execute(
                systemconfig.update().where(systemconfig.c.key == key).values(
                    value=_storages
                )
            )


def downgrade() -> None:
    """
    本版本不执行回退操作，保留升级后的结构与数据。
    """
    pass
