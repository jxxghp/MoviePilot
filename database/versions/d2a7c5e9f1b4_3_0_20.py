"""3.0.20 增加订阅搜索站点预算与批次启动抖动。

Revision ID: d2a7c5e9f1b4
Revises: c1f4a8d2e6b9
Create Date: 2026-09-01
"""

# Alembic 的 op 是运行期代理，静态分析无法看到实际操作方法。
# pylint: disable=no-member

import sqlalchemy as sa
from alembic import op

revision = "d2a7c5e9f1b4"
down_revision = "c1f4a8d2e6b9"
branch_labels = None
depends_on = None

_TASK_TABLE = "subscriptionsearchtask"
_BUDGET_TABLE = "subscriptionsitebudget"


def _table_names() -> set[str]:
    """返回当前数据库表名集合。"""
    return set(sa.inspect(op.get_bind()).get_table_names())


def _column_names(table_name: str) -> set[str]:
    """返回指定表的当前字段名集合。"""
    return {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns(table_name)
    }


def upgrade() -> None:
    """创建单站点预算，并让搜索任务支持持久启动抖动。"""
    tables = _table_names()
    if _TASK_TABLE in tables and "available_at" not in _column_names(_TASK_TABLE):
        with op.batch_alter_table(_TASK_TABLE) as batch_op:
            batch_op.add_column(sa.Column("available_at", sa.String(length=40), nullable=True))
            batch_op.drop_index("ix_subscriptionsearchtask_claim")
            batch_op.create_index(
                "ix_subscriptionsearchtask_claim",
                [
                    "state",
                    "priority",
                    "available_at",
                    "lease_expires_at",
                    "created_at",
                    "id",
                ],
            )
        op.execute(
            sa.text(
                "UPDATE subscriptionsearchtask "
                "SET available_at = created_at WHERE available_at IS NULL"
            )
        )
    if _BUDGET_TABLE not in tables:
        op.create_table(
            _BUDGET_TABLE,
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("site_id", sa.Integer(), nullable=False),
            sa.Column("lease_owner", sa.String(length=128), nullable=True),
            sa.Column("lease_token", sa.String(length=64), nullable=True),
            sa.Column("lease_expires_at", sa.String(length=40), nullable=True),
            sa.Column("next_allowed_at", sa.String(length=40), nullable=False),
            sa.Column("consecutive_failures", sa.Integer(), nullable=False),
            sa.Column("success_streak", sa.Integer(), nullable=False),
            sa.Column("last_outcome", sa.String(length=32), nullable=True),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("updated_at", sa.String(length=40), nullable=False),
            sa.UniqueConstraint("site_id", name="uq_subscriptionsitebudget_site_id"),
        )
        op.create_index(
            "ix_subscriptionsitebudget_ready",
            _BUDGET_TABLE,
            ["next_allowed_at", "lease_expires_at", "site_id"],
        )


def downgrade() -> None:
    """移除站点预算与任务启动抖动。"""
    tables = _table_names()
    if _BUDGET_TABLE in tables:
        op.drop_table(_BUDGET_TABLE)
    if _TASK_TABLE in tables and "available_at" in _column_names(_TASK_TABLE):
        with op.batch_alter_table(_TASK_TABLE) as batch_op:
            batch_op.drop_index("ix_subscriptionsearchtask_claim")
            batch_op.drop_column("available_at")
            batch_op.create_index(
                "ix_subscriptionsearchtask_claim",
                ["state", "priority", "lease_expires_at", "created_at", "id"],
            )
