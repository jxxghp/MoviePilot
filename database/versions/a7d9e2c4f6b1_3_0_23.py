"""3.0.23 增加订阅下载交付目标范围。

Revision ID: a7d9e2c4f6b1
Revises: f3c8a1d6b2e9
Create Date: 2026-09-01
"""

# Alembic 的 op 是运行期代理，静态分析无法看到实际操作方法。
# pylint: disable=no-member

import sqlalchemy as sa
from alembic import op

revision = "a7d9e2c4f6b1"
down_revision = "f3c8a1d6b2e9"
branch_labels = None
depends_on = None

_TABLE = "subscriptiondownloadsubmission"


def _column_names() -> set[str]:
    """返回当前订阅下载提交列名集合。"""
    inspector = sa.inspect(op.get_bind())
    if _TABLE not in set(inspector.get_table_names()):
        return set()
    return {column["name"] for column in inspector.get_columns(_TABLE)}


def upgrade() -> None:
    """为存量提交增加兼容交付范围，新键会写入实际下载目标。"""
    columns = _column_names()
    if columns and "delivery_scope" not in columns:
        op.add_column(
            _TABLE,
            sa.Column(
                "delivery_scope",
                sa.Text(),
                nullable=False,
                server_default="legacy",
            ),
        )


def downgrade() -> None:
    """移除交付范围字段，保留原提交账本与唯一键。"""
    if "delivery_scope" in _column_names():
        op.drop_column(_TABLE, "delivery_scope")
