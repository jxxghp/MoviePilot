"""3.0.33 增加 Agent 写工具调用的持久回执与原子防重身份。"""

import sqlalchemy as sa
from alembic import op

revision = "b2d4f6a8c1e3"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def _id_column(dialect_name: str) -> sa.Column:
    """与宿主当前模型使用同一种主键自增语义。"""
    if dialect_name == "postgresql":
        return sa.Column("id", sa.Integer(), sa.Identity(start=1, cycle=True), primary_key=True)
    return sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True)


def upgrade() -> None:
    """兼容已有 SQLite/PostgreSQL 与 create_all 已建表的全新实例。"""
    bind = op.get_bind()
    if "agentinvocation" not in sa.inspect(bind).get_table_names():
        op.create_table(
            "agentinvocation",
            _id_column(bind.dialect.name),
            sa.Column("principal_id", sa.String(255), nullable=False),
            sa.Column("session_id", sa.String(255), nullable=False),
            sa.Column("invocation_id", sa.String(255), nullable=False),
            sa.Column("tool_name", sa.String(128), nullable=False),
            sa.Column("arguments_digest", sa.String(64), nullable=False),
            sa.Column("claim_token", sa.String(32), nullable=False),
            sa.Column("status", sa.String(16), nullable=False),
            sa.Column("summary", sa.String(128), nullable=False),
            sa.Column("created_at", sa.String(), nullable=False),
            sa.Column("updated_at", sa.String(), nullable=False),
            sa.CheckConstraint(
                "status IN ('running', 'succeeded', 'failed', 'pending', 'unknown')",
                name="ck_agentinvocation_status",
            ),
        )
    existing = {index["name"] for index in sa.inspect(bind).get_indexes("agentinvocation")}
    for name, columns, unique in (
        ("ix_agentinvocation_identity", ["principal_id", "session_id", "invocation_id"], True),
        ("ix_agentinvocation_status_updated_id", ["status", "updated_at", "id"], False),
    ):
        if name not in existing:
            op.create_index(name, "agentinvocation", columns, unique=unique)


def downgrade() -> None:
    """仅移除新增调用回执表，不修改会话与工具业务数据。"""
    if "agentinvocation" in sa.inspect(op.get_bind()).get_table_names():
        op.drop_table("agentinvocation")
