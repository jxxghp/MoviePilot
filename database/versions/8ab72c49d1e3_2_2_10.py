"""
2.2.10 新增 Agent 会话历史表

版本标识： 8ab72c49d1e3
前置版本： 7c1a2b3d4e5f
创建时间： 2026-06-18
"""

from alembic import op
import sqlalchemy as sa

revision = "8ab72c49d1e3"
down_revision = "7c1a2b3d4e5f"
branch_labels = None
depends_on = None


def _has_table(inspector: sa.Inspector, table_name: str) -> bool:
    """
    检查数据表是否已存在。
    """
    return table_name in inspector.get_table_names()


def upgrade() -> None:
    """
    升级数据库结构。
    """
    inspector = sa.inspect(op.get_bind())
    if _has_table(inspector, "agentchat"):
        return

    op.create_table(
        "agentchat",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("client_session_id", sa.String(), nullable=True),
        sa.Column("user_id", sa.String(), nullable=True),
        sa.Column("username", sa.String(), nullable=True),
        sa.Column("channel", sa.String(), nullable=True),
        sa.Column("source", sa.String(), nullable=True),
        sa.Column("original_chat_id", sa.String(), nullable=True),
        sa.Column("title", sa.String(), nullable=True),
        sa.Column("preview", sa.String(), nullable=True),
        sa.Column("agent_messages", sa.JSON(), nullable=True),
        sa.Column("display_messages", sa.JSON(), nullable=True),
        sa.Column("message_count", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.String(), nullable=True),
        sa.Column("updated_at", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_agentchat_session_user",
        "agentchat",
        ["session_id", "user_id"],
    )
    op.create_index(
        "ix_agentchat_user_updated",
        "agentchat",
        ["user_id", "updated_at", "id"],
    )
    op.create_index(
        "ix_agentchat_channel_updated",
        "agentchat",
        ["channel", "updated_at", "id"],
    )


def downgrade() -> None:
    """
    回滚数据库结构。
    """
    inspector = sa.inspect(op.get_bind())
    if not _has_table(inspector, "agentchat"):
        return
    op.drop_index("ix_agentchat_channel_updated", table_name="agentchat")
    op.drop_index("ix_agentchat_user_updated", table_name="agentchat")
    op.drop_index("ix_agentchat_session_user", table_name="agentchat")
    op.drop_table("agentchat")
