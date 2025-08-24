"""2.1.3

Revision ID: 4b544f5d3b07
Revises: 610bb05ddeef
Create Date: 2025-04-03 11:21:42.780337

"""
import contextlib

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import sqlite

# revision identifiers, used by Alembic.
revision = '4b544f5d3b07'
down_revision = '610bb05ddeef'
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    # 检查并添加 downloadhistory.episode_group
    dh_columns = inspector.get_columns('downloadhistory')
    if not any(c['name'] == 'episode_group' for c in dh_columns):
        op.add_column('downloadhistory', sa.Column('episode_group', sa.String, nullable=True))

    # 检查并添加 subscribe.episode_group
    s_columns = inspector.get_columns('subscribe')
    if not any(c['name'] == 'episode_group' for c in s_columns):
        op.add_column('subscribe', sa.Column('episode_group', sa.String, nullable=True))

    # 检查并添加 subscribehistory.episode_group
    sh_columns = inspector.get_columns('subscribehistory')
    if not any(c['name'] == 'episode_group' for c in sh_columns):
        op.add_column('subscribehistory', sa.Column('episode_group', sa.String, nullable=True))

    # 检查并添加 transferhistory.episode_group
    th_columns = inspector.get_columns('transferhistory')
    if not any(c['name'] == 'episode_group' for c in th_columns):
        op.add_column('transferhistory', sa.Column('episode_group', sa.String, nullable=True))


def downgrade() -> None:
    pass
