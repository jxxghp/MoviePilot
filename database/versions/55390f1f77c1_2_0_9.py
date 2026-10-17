"""
2.0.9 为整理历史补齐可空的下载器名称字段。

版本标识： 55390f1f77c1
前置版本： bf28a012734c
创建时间： 2024-12-24 13:29:32.225532
"""
import contextlib

import sqlalchemy as sa
from alembic import op

# Alembic 使用以下标识连接迁移版本链。
revision = '55390f1f77c1'
down_revision = 'bf28a012734c'
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    为整理历史补齐可空的下载器名称字段。
    """
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    columns = inspector.get_columns('transferhistory')
    if not any(c['name'] == 'downloader' for c in columns):
        op.add_column('transferhistory', sa.Column('downloader', sa.String(), nullable=True))


def downgrade() -> None:
    """
    本版本不执行回退操作，保留升级后的结构与数据。
    """
    pass
