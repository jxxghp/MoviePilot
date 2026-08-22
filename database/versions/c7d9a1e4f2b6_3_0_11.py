"""3.0.11 add durable side-effect outbox.

合并时与本仓已推进到 3.0.10 的插件实例配置迁移各自独立地接在 3.0.7 之后，
形成两个 head；改接到 3.0.10 之后使迁移链保持单一线性，符合
``_validate_migration_lineage`` 只允许一个 head 的约束。

Revision ID: c7d9a1e4f2b6
Revises: 27c3b2eb9b1e
Create Date: 2026-08-21
"""

from alembic import op
import sqlalchemy as sa


revision = "c7d9a1e4f2b6"
down_revision = "27c3b2eb9b1e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """创建可认领、有限重试并进入 dead letter 的 outbox 表。"""
    # fresh 安装先由 metadata.create_all 建当前结构，再补跑 Alembic 链；此时只需推进 revision。
    if "outboxmessage" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "outboxmessage",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("event_key", sa.String(length=255), nullable=False),
        sa.Column("topic", sa.String(length=100), nullable=False),
        sa.Column("payload_version", sa.Integer(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("next_retry_at", sa.String(length=40), nullable=False),
        sa.Column("lease_until", sa.String(length=40), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.String(length=40), nullable=False),
        sa.Column("completed_at", sa.String(length=40), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_key", name="uq_outboxmessage_event_key"),
    )
    op.create_index(
        "ix_outboxmessage_claim",
        "outboxmessage",
        ["status", "next_retry_at", "lease_until"],
        unique=False,
    )


def downgrade() -> None:
    """删除 outbox；未投递副作用会不可逆丢失，降级前必须确认队列为空。"""
    if "outboxmessage" not in sa.inspect(op.get_bind()).get_table_names():
        return
    op.drop_index("ix_outboxmessage_claim", table_name="outboxmessage")
    op.drop_table("outboxmessage")
