"""
2.1.8 为工作流补齐触发方式、事件类型和事件条件字段。

版本标识： 4666ce24a443
前置版本： 3891a5e722a1
创建时间： 2025-07-22 13:54:04.196126
"""
import contextlib

import sqlalchemy as sa
from alembic import op

# Alembic 使用以下标识连接迁移版本链。
revision = '4666ce24a443'
down_revision = '3891a5e722a1'
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    为工作流补齐触发方式、事件类型和事件条件字段。
    """
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    columns = inspector.get_columns('workflow')

    if not any(c['name'] == 'trigger_type' for c in columns):
        op.add_column('workflow', sa.Column('trigger_type', sa.String(), nullable=True, default='timer'))

    if not any(c['name'] == 'event_type' for c in columns):
        op.add_column('workflow', sa.Column('event_type', sa.String(), nullable=True))

    if not any(c['name'] == 'event_conditions' for c in columns):
        op.add_column('workflow', sa.Column('event_conditions', sa.JSON(), nullable=True, default={}))


def downgrade() -> None:
    """
    本版本不执行回退操作，保留升级后的结构与数据。
    """
    pass
