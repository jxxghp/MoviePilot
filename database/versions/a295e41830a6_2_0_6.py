"""
2.0.6 已有存储配置中尚无 AList 时追加默认 AList 配置。

版本标识： a295e41830a6
前置版本： ecf3c693fdf3
创建时间： 2024-11-14 12:49:13.838120
"""

from alembic import op
import sqlalchemy as sa

# Alembic 使用以下标识连接迁移版本链。
revision = 'a295e41830a6'
down_revision = 'ecf3c693fdf3'
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    已有存储配置中尚无 AList 时追加默认 AList 配置。
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
    if _storages and "alist" not in [storage["type"] for storage in _storages]:
        _storages.append({
            "type": "alist",
            "name": "AList",
            "config": {}
        })
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
