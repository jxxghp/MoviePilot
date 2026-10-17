"""
2.0.4 将资源排序规则统一设为种子、上传量与做种数的顺序。

版本标识： a73f2dbf5c09
前置版本： e2dbe1421fa4
创建时间： 2024-10-16 15:05:01.775429
"""

from alembic import op
import sqlalchemy as sa

# Alembic 使用以下标识连接迁移版本链。
revision = 'a73f2dbf5c09'
down_revision = 'e2dbe1421fa4'
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    将资源排序规则统一设为种子、上传量与做种数的顺序。
    """
    systemconfig = sa.table(
        "systemconfig",
        sa.column("key", sa.String()),
        sa.column("value", sa.JSON()),
    )
    connection = op.get_bind()
    key = "TorrentsPriority"
    value = ["torrent", "upload", "seeder"]
    row = connection.execute(
        sa.select(systemconfig.c.value).where(systemconfig.c.key == key)
    ).first()
    if row and row[0] != value:
        connection.execute(
            systemconfig.update().where(systemconfig.c.key == key).values(
                value=value
            )
        )
    elif not row:
        connection.execute(systemconfig.insert().values(key=key, value=value))


def downgrade() -> None:
    """
    本版本不执行回退操作，保留升级后的结构与数据。
    """
    pass
