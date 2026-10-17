"""
2.0.8 为下载历史补齐可空的下载器名称字段。

版本标识： bf28a012734c
前置版本： eaf9cbc49027
创建时间： 2024-12-23 18:29:31.202143
"""
import contextlib

import sqlalchemy as sa
from alembic import op

# Alembic 使用以下标识连接迁移版本链。
revision = 'bf28a012734c'
down_revision = 'eaf9cbc49027'
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    为下载历史补齐可空的下载器名称字段。
    """
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    columns = inspector.get_columns('downloadhistory')
    if not any(c['name'] == 'downloader' for c in columns):
        op.add_column('downloadhistory', sa.Column('downloader', sa.String(), nullable=True))


def downgrade() -> None:
    """
    本版本不执行回退操作，保留升级后的结构与数据。
    """
    pass
