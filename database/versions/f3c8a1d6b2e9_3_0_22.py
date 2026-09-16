"""3.0.22 增加订阅搜索业务阶段与当前站点。

Revision ID: f3c8a1d6b2e9
Revises: e1b6d4f8a2c7
Create Date: 2026-09-01
"""

# Alembic 的 op 是运行期代理，静态分析无法看到实际操作方法。
# pylint: disable=no-member

import sqlalchemy as sa
from alembic import op

revision = "f3c8a1d6b2e9"
down_revision = "e1b6d4f8a2c7"
branch_labels = None
depends_on = None

_TABLE = "subscriptionsearchtask"


def _column_names() -> set[str]:
    """返回当前订阅搜索任务列名集合。"""
    inspector = sa.inspect(op.get_bind())
    if _TABLE not in set(inspector.get_table_names()):
        return set()
    return {column["name"] for column in inspector.get_columns(_TABLE)}


def upgrade() -> None:
    """为存量队列增加带默认值的可观察阶段字段。"""
    columns = _column_names()
    if not columns:
        return
    if "phase" not in columns:
        op.add_column(
            _TABLE,
            sa.Column(
                "phase",
                sa.String(length=32),
                nullable=False,
                server_default="queued",
            ),
        )
    if "current_site_id" not in columns:
        op.add_column(
            _TABLE,
            sa.Column("current_site_id", sa.Integer(), nullable=True),
        )


def downgrade() -> None:
    """移除业务阶段字段，保留原搜索队列事实。"""
    columns = _column_names()
    if "current_site_id" in columns:
        op.drop_column(_TABLE, "current_site_id")
    if "phase" in columns:
        op.drop_column(_TABLE, "phase")
