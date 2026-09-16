"""3.0.19 增加订阅搜索持久批次与任务队列。

Revision ID: c1f4a8d2e6b9
Revises: a9d4f2c7e6b1
Create Date: 2026-09-01
"""

# Alembic 的 op 是运行期代理，静态分析无法看到实际操作方法。
# pylint: disable=no-member

import sqlalchemy as sa
from alembic import op

revision = "c1f4a8d2e6b9"
down_revision = "a9d4f2c7e6b1"
branch_labels = None
depends_on = None

_BATCH_TABLE = "subscriptionsearchbatch"
_TASK_TABLE = "subscriptionsearchtask"


def _table_names() -> set[str]:
    """返回当前数据库表名集合。"""
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    """创建跨 SQLite/PostgreSQL 一致的订阅搜索治理表。"""
    tables = _table_names()
    if _BATCH_TABLE not in tables:
        op.create_table(
            _BATCH_TABLE,
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("batch_id", sa.String(length=64), nullable=False),
            sa.Column("source", sa.String(length=32), nullable=False),
            sa.Column("state", sa.String(length=32), nullable=False),
            sa.Column("priority", sa.Integer(), nullable=False),
            sa.Column("total_count", sa.Integer(), nullable=False),
            sa.Column("finished_count", sa.Integer(), nullable=False),
            sa.Column("failed_count", sa.Integer(), nullable=False),
            sa.Column("cancelled_count", sa.Integer(), nullable=False),
            sa.Column("cancel_requested", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.String(length=40), nullable=False),
            sa.Column("updated_at", sa.String(length=40), nullable=False),
            sa.Column("started_at", sa.String(length=40), nullable=True),
            sa.Column("finished_at", sa.String(length=40), nullable=True),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.UniqueConstraint("batch_id", name="uq_subscriptionsearchbatch_batch_id"),
        )
        op.create_index(
            "ix_subscriptionsearchbatch_state_created",
            _BATCH_TABLE,
            ["state", "created_at", "id"],
        )
    if _TASK_TABLE not in tables:
        op.create_table(
            _TASK_TABLE,
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("task_id", sa.String(length=64), nullable=False),
            sa.Column("batch_id", sa.String(length=64), nullable=False),
            sa.Column("subscription_id", sa.Integer(), nullable=False),
            sa.Column("active_key", sa.String(length=128), nullable=True),
            sa.Column("source", sa.String(length=32), nullable=False),
            sa.Column("priority", sa.Integer(), nullable=False),
            sa.Column("position", sa.Integer(), nullable=False),
            sa.Column("state", sa.String(length=32), nullable=False),
            sa.Column("attempt_count", sa.Integer(), nullable=False),
            sa.Column("cancel_requested", sa.Integer(), nullable=False),
            sa.Column("lease_owner", sa.String(length=128), nullable=True),
            sa.Column("lease_token", sa.String(length=64), nullable=True),
            sa.Column("lease_expires_at", sa.String(length=40), nullable=True),
            sa.Column("created_at", sa.String(length=40), nullable=False),
            sa.Column("updated_at", sa.String(length=40), nullable=False),
            sa.Column("started_at", sa.String(length=40), nullable=True),
            sa.Column("finished_at", sa.String(length=40), nullable=True),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.UniqueConstraint("task_id", name="uq_subscriptionsearchtask_task_id"),
            sa.UniqueConstraint("active_key", name="uq_subscriptionsearchtask_active_key"),
        )
        op.create_index(
            "ix_subscriptionsearchtask_claim",
            _TASK_TABLE,
            ["state", "priority", "lease_expires_at", "created_at", "id"],
        )
        op.create_index(
            "ix_subscriptionsearchtask_batch_position",
            _TASK_TABLE,
            ["batch_id", "position", "id"],
        )
        op.create_index(
            "ix_subscriptionsearchtask_subscription",
            _TASK_TABLE,
            ["subscription_id", "created_at", "id"],
        )


def downgrade() -> None:
    """移除订阅搜索治理表。"""
    tables = _table_names()
    if _TASK_TABLE in tables:
        op.drop_table(_TASK_TABLE)
    if _BATCH_TABLE in tables:
        op.drop_table(_BATCH_TABLE)
