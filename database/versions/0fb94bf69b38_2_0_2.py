"""
2.0.2 为站点用户数据增加可空的站点名称字段。

版本标识： 0fb94bf69b38
前置版本： 262735d025da
创建时间： 2024-09-30 10:03:58.546036
"""
import contextlib

from alembic import op
import sqlalchemy as sa


# Alembic 使用以下标识连接迁移版本链。
revision = '0fb94bf69b38'
down_revision = '262735d025da'
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    为站点用户数据增加可空的站点名称字段。
    """
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    columns = inspector.get_columns('siteuserdata')
    if not any(c['name'] == 'name' for c in columns):
        op.add_column('siteuserdata', sa.Column('name', sa.String(), nullable=True))


def downgrade() -> None:
    """
    本版本不执行回退操作，保留升级后的结构与数据。
    """
    pass
