"""
为 Agent 计划与运行记录增加独立执行账号，旧任务须显式重新绑定。
"""

import sqlalchemy as sa
from alembic import op

revision = "97ad7239c1e0"
down_revision = "4f62a73e81c9"
branch_labels = None
depends_on = None

_TABLES = ("agenttask", "agenttaskrun")
_COLUMNS = ("execution_user_id", "execution_user_identity")


def upgrade() -> None:
    """
    增加可空用户主键，保留旧记录且不根据昵称或渠道数字猜测执行账号。
    """
    connection = op.get_bind()
    for table in _TABLES:
        inspector = sa.inspect(connection)
        if table not in inspector.get_table_names():
            continue
        existing = {column["name"] for column in inspector.get_columns(table)}
        for name, column_type in zip(_COLUMNS, (sa.Integer(), sa.String(32))):
            if name not in existing:
                op.add_column(table, sa.Column(name, column_type, nullable=True))


def downgrade() -> None:
    """
    删除独立执行账号列，保留原渠道归属和任务运行历史。
    """
    connection = op.get_bind()
    for table in reversed(_TABLES):
        inspector = sa.inspect(connection)
        if table not in inspector.get_table_names():
            continue
        existing = {column["name"] for column in inspector.get_columns(table)}
        for name in reversed(_COLUMNS):
            if name in existing:
                op.drop_column(table, name)
