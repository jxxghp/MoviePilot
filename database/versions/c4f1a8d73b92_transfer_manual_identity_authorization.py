"""
增加人工确认媒体身份的一次性整理授权表。

表只记录授权与消费审计，不改动既有整理历史、失败任务或重试计数，
升级和回退都不触碰业务数据。
"""

import sqlalchemy as sa
from alembic import op

revision = "c4f1a8d73b92"
down_revision = "a7383c1d9e20"
branch_labels = None
depends_on = None

TABLE = "transfermanualidentityauthorization"


def upgrade() -> None:
    """
    兼容首次建表和已有数据库，重复执行时保持幂等。
    """
    inspector = sa.inspect(op.get_bind())
    if TABLE in inspector.get_table_names():
        return
    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("history_id", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.String(64), nullable=False),
        sa.Column("settlement_revision", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("issued_by", sa.String(128), nullable=False),
        sa.Column("issued_at", sa.String(40), nullable=False),
        sa.Column("expires_at", sa.String(40), nullable=False),
        sa.Column("used_at", sa.String(40), nullable=True),
        sa.Column("used_by", sa.String(128), nullable=True),
        sa.Column("media_source", sa.String(32), nullable=True),
        sa.Column("media_id", sa.String(64), nullable=True),
    )
    op.create_index(
        "ix_transfermanualidentityauthorization_history",
        TABLE,
        ["history_id", "task_id"],
    )
    op.create_index(
        "ix_transfermanualidentityauthorization_task",
        TABLE,
        ["task_id"],
    )


def downgrade() -> None:
    """
    只移除授权审计表，保留整理历史与任务状态。
    """
    inspector = sa.inspect(op.get_bind())
    if TABLE not in inspector.get_table_names():
        return
    op.drop_table(TABLE)
