"""2.1.0

Revision ID: ca5461f314f2
Revises: 55390f1f77c1
Create Date: 2025-02-06 18:28:00.644571

"""
import contextlib

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = 'ca5461f314f2'
down_revision = '55390f1f77c1'
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    # 检查并添加 subscribe.mediaid
    s_columns = inspector.get_columns('subscribe')
    if not any(c['name'] == 'mediaid' for c in s_columns):
        op.add_column('subscribe', sa.Column('mediaid', sa.String(), nullable=True))

    # 检查并创建索引
    s_indexes = inspector.get_indexes('subscribe')
    if not any(i['name'] == 'ix_subscribe_mediaid' for i in s_indexes):
        op.create_index('ix_subscribe_mediaid', 'subscribe', ['mediaid'], unique=False)

    # 检查并添加 subscribehistory.mediaid
    sh_columns = inspector.get_columns('subscribehistory')
    if not any(c['name'] == 'mediaid' for c in sh_columns):
        op.add_column('subscribehistory', sa.Column('mediaid', sa.String(), nullable=True))


def downgrade() -> None:
    pass
