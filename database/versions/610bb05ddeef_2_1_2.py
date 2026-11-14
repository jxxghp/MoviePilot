"""
2.1.2 为工作流增加保存连线结构的 JSON 字段。

版本标识： 610bb05ddeef
前置版本： 279a949d81b6
创建时间： 2025-02-24 07:52:00.042837
"""
import contextlib

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import sqlite

# Alembic 使用以下标识连接迁移版本链。
revision = '610bb05ddeef'
down_revision = '279a949d81b6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    为工作流增加保存连线结构的 JSON 字段。
    """
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    columns = inspector.get_columns('workflow')
    if not any(c['name'] == 'flows' for c in columns):
        op.add_column('workflow', sa.Column('flows', sa.JSON(), nullable=True))


def downgrade() -> None:
    """
    本版本不执行回退操作，保留升级后的结构与数据。
    """
    pass
