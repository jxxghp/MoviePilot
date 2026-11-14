"""
2.1.0 为订阅和完成历史补齐旧媒体标识字段，并为活动订阅建立查询索引。

版本标识： ca5461f314f2
前置版本： 55390f1f77c1
创建时间： 2025-02-06 18:28:00.644571
"""
import contextlib

import sqlalchemy as sa
from alembic import op

# Alembic 使用以下标识连接迁移版本链。
revision = 'ca5461f314f2'
down_revision = '55390f1f77c1'
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    为订阅和完成历史补齐旧媒体标识字段，并为活动订阅建立查询索引。
    """
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    s_columns = inspector.get_columns('subscribe')
    if not any(c['name'] == 'mediaid' for c in s_columns):
        op.add_column('subscribe', sa.Column('mediaid', sa.String(), nullable=True))

    s_indexes = inspector.get_indexes('subscribe')
    if not any(i['name'] == 'ix_subscribe_mediaid' for i in s_indexes):
        op.create_index('ix_subscribe_mediaid', 'subscribe', ['mediaid'], unique=False)

    sh_columns = inspector.get_columns('subscribehistory')
    if not any(c['name'] == 'mediaid' for c in sh_columns):
        op.add_column('subscribehistory', sa.Column('mediaid', sa.String(), nullable=True))


def downgrade() -> None:
    """
    本版本不执行回退操作，保留升级后的结构与数据。
    """
    pass
