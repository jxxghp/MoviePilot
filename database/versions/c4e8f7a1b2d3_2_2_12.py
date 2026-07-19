"""2.2.12
新增 Agent 自主定时任务表

Revision ID: c4e8f7a1b2d3
Revises: b7d4a9c2e6f1
Create Date: 2026-07-19
"""

from alembic import op
import sqlalchemy as sa

revision = "c4e8f7a1b2d3"
down_revision = "b7d4a9c2e6f1"
branch_labels = None
depends_on = None


def _has_table(inspector: sa.Inspector, table_name: str) -> bool:
    """检查数据表是否已存在。"""
    return table_name in inspector.get_table_names()


def upgrade() -> None:
    """升级数据库结构。"""
    inspector = sa.inspect(op.get_bind())
    if _has_table(inspector, "agenttask"):
        return

    op.create_table(
        "agenttask",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("trigger_type", sa.String(), nullable=False),
        sa.Column("cron_expression", sa.String(), nullable=True),
        sa.Column("run_at", sa.String(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("username", sa.String(), nullable=True),
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("channel", sa.String(), nullable=True),
        sa.Column("source", sa.String(), nullable=True),
        sa.Column("original_chat_id", sa.String(), nullable=True),
        sa.Column("last_status", sa.String(), nullable=False),
        sa.Column("last_run_at", sa.String(), nullable=True),
        sa.Column("last_result", sa.Text(), nullable=True),
        sa.Column("run_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_agenttask_enabled", "agenttask", ["enabled"])
    op.create_index(
        "ix_agenttask_user_created",
        "agenttask",
        ["user_id", "created_at", "id"],
    )


def downgrade() -> None:
    """回滚数据库结构。"""
    inspector = sa.inspect(op.get_bind())
    if not _has_table(inspector, "agenttask"):
        return
    op.drop_index("ix_agenttask_user_created", table_name="agenttask")
    op.drop_index("ix_agenttask_enabled", table_name="agenttask")
    op.drop_table("agenttask")
