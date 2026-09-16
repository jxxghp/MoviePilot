"""3.0.21 增加订阅下载幂等与待对账提交账本。

Revision ID: e1b6d4f8a2c7
Revises: d2a7c5e9f1b4
Create Date: 2026-09-01
"""

# Alembic 的 op 是运行期代理，静态分析无法看到实际操作方法。
# pylint: disable=no-member

import sqlalchemy as sa
from alembic import op

revision = "e1b6d4f8a2c7"
down_revision = "d2a7c5e9f1b4"
branch_labels = None
depends_on = None

_TABLE = "subscriptiondownloadsubmission"


def _table_names() -> set[str]:
    """返回当前数据库表名集合。"""
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    """创建向后兼容的独立订阅下载提交账本。"""
    if _TABLE in _table_names():
        return
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("idempotency_key", sa.String(length=64), nullable=False),
        sa.Column("subscription_id", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.String(length=64), nullable=True),
        sa.Column("logical_identity", sa.Text(), nullable=False),
        sa.Column("resource_key", sa.Text(), nullable=False),
        sa.Column("coverage", sa.Text(), nullable=False),
        sa.Column("mode", sa.String(length=32), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("attempt_token", sa.String(length=64), nullable=True),
        sa.Column("downloader", sa.String(length=128), nullable=True),
        sa.Column("download_hash", sa.String(length=256), nullable=True),
        sa.Column("available_at", sa.String(length=40), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.String(length=40), nullable=False),
        sa.Column("updated_at", sa.String(length=40), nullable=False),
        sa.Column("started_at", sa.String(length=40), nullable=True),
        sa.Column("finished_at", sa.String(length=40), nullable=True),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_subscriptiondownloadsubmission_idempotency_key",
        ),
    )
    op.create_index(
        "ix_subscriptiondownloadsubmission_task_state",
        _TABLE,
        ["task_id", "state", "id"],
    )
    op.create_index(
        "ix_subscriptiondownloadsubmission_subscription_state",
        _TABLE,
        ["subscription_id", "state", "updated_at", "id"],
    )


def downgrade() -> None:
    """移除订阅下载提交账本，不改动旧下载历史。"""
    if _TABLE in _table_names():
        op.drop_table(_TABLE)
