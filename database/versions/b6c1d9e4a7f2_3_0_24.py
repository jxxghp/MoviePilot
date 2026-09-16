"""3.0.24 增加订阅搜索批次跳过计数。"""

# Alembic 的 op 是运行期代理，静态分析无法看到实际操作方法。
# pylint: disable=no-member

import sqlalchemy as sa
from alembic import op

revision = "b6c1d9e4a7f2"
down_revision = "a7d9e2c4f6b1"
branch_labels = None
depends_on = None

_TABLE = "subscriptionsearchbatch"


def _column_names() -> set[str]:
    """返回当前订阅搜索批次字段名集合。"""
    inspector = sa.inspect(op.get_bind())
    if _TABLE not in set(inspector.get_table_names()):
        return set()
    return {column["name"] for column in inspector.get_columns(_TABLE)}


def upgrade() -> None:
    """为存量批次增加跳过计数，保持旧数据库可重复升级。"""
    if _TABLE in set(sa.inspect(op.get_bind()).get_table_names()) and "skipped_count" not in _column_names():
        op.add_column(
            _TABLE,
            sa.Column("skipped_count", sa.Integer(), nullable=False, server_default="0"),
        )


def downgrade() -> None:
    """移除批次跳过计数，保留原搜索任务与批次记录。"""
    if "skipped_count" in _column_names():
        op.drop_column(_TABLE, "skipped_count")
