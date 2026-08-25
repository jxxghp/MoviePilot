"""3.0.9 add installed plugin source identities.

Revision ID: d2e4f6a8b0c1
Revises: c7d9a1e4f2b6
Create Date: 2026-08-25
"""

from alembic import op
import sqlalchemy as sa


revision = "d2e4f6a8b0c1"
down_revision = "c7d9a1e4f2b6"
branch_labels = None
depends_on = None


def _id_column(dialect_name: str) -> sa.Column:
    """保持 PostgreSQL Identity 与 SQLite 整数主键的当前模型语义一致。"""
    if dialect_name == "postgresql":
        return sa.Column(
            "id",
            sa.Integer(),
            sa.Identity(start=1, cycle=True),
            nullable=False,
        )
    return sa.Column("id", sa.Integer(), nullable=False)


def upgrade() -> None:
    """创建一插件一行、支持 revision 条件写的来源身份表。"""
    if "pluginidentity" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "pluginidentity",
        _id_column(op.get_bind().dialect.name),
        sa.Column("plugin_id", sa.String(length=128), nullable=False),
        sa.Column("normalized_plugin_id", sa.String(length=128), nullable=False),
        sa.Column("trusted_source_type", sa.String(length=20), nullable=False),
        sa.Column("trusted_source_key", sa.String(length=255), nullable=True),
        sa.Column("binding_basis", sa.String(length=32), nullable=False),
        sa.Column("payload_source_type", sa.String(length=20), nullable=False),
        sa.Column("payload_source_key", sa.String(length=255), nullable=True),
        sa.Column("declared_version", sa.String(length=64), nullable=True),
        sa.Column("package_generation", sa.String(length=8), nullable=True),
        sa.Column("system_version", sa.String(length=128), nullable=True),
        sa.Column("supports_v3", sa.Boolean(), nullable=True),
        sa.Column("supports_v3t", sa.Boolean(), nullable=True),
        sa.Column("payload_receipt", sa.String(length=71), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.String(length=40), nullable=False),
        sa.Column("updated_at", sa.String(length=40), nullable=False),
        sa.Column("bound_at", sa.String(length=40), nullable=True),
        sa.Column("payload_applied_at", sa.String(length=40), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "normalized_plugin_id",
            name="uq_pluginidentity_normalized_plugin_id",
        ),
        sa.CheckConstraint(
            "normalized_plugin_id <> '' "
            "AND normalized_plugin_id = lower(normalized_plugin_id)",
            name="ck_pluginidentity_normalized_plugin_id",
        ),
        sa.CheckConstraint(
            "revision >= 1",
            name="ck_pluginidentity_revision",
        ),
    )


def downgrade() -> None:
    """删除尚未启用生产写入的插件来源身份表。"""
    if "pluginidentity" not in sa.inspect(op.get_bind()).get_table_names():
        return
    op.drop_table("pluginidentity")
