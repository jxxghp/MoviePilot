"""
2.1.3 为下载、整理、订阅与订阅历史补齐剧集组字段。

版本标识： 4b544f5d3b07
前置版本： 610bb05ddeef
创建时间： 2025-04-03 11:21:42.780337
"""
import contextlib

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import sqlite

# Alembic 使用以下标识连接迁移版本链。
revision = '4b544f5d3b07'
down_revision = '610bb05ddeef'
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    为下载、整理、订阅与订阅历史补齐剧集组字段。
    """
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    dh_columns = inspector.get_columns('downloadhistory')
    if not any(c['name'] == 'episode_group' for c in dh_columns):
        op.add_column('downloadhistory', sa.Column('episode_group', sa.String, nullable=True))

    s_columns = inspector.get_columns('subscribe')
    if not any(c['name'] == 'episode_group' for c in s_columns):
        op.add_column('subscribe', sa.Column('episode_group', sa.String, nullable=True))

    sh_columns = inspector.get_columns('subscribehistory')
    if not any(c['name'] == 'episode_group' for c in sh_columns):
        op.add_column('subscribehistory', sa.Column('episode_group', sa.String, nullable=True))

    th_columns = inspector.get_columns('transferhistory')
    if not any(c['name'] == 'episode_group' for c in th_columns):
        op.add_column('transferhistory', sa.Column('episode_group', sa.String, nullable=True))


def downgrade() -> None:
    """
    本版本不执行回退操作，保留升级后的结构与数据。
    """
    pass
