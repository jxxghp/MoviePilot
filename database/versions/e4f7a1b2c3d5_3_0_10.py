"""3.0.10 add durable plugin installation transactions.

Revision ID: e4f7a1b2c3d5
Revises: d2e4f6a8b0c1
Create Date: 2026-08-25
"""

import sqlalchemy as sa
from alembic import op

revision = "e4f7a1b2c3d5"
down_revision = "d2e4f6a8b0c1"
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
    """创建单插件安装事务状态存储。"""
    inspector = sa.inspect(op.get_bind())
    if "plugininstallation" in inspector.get_table_names():
        return
    op.create_table(
        "plugininstallation",
        _id_column(op.get_bind().dialect.name),
        sa.Column("transaction_id", sa.String(length=128), nullable=False),
        sa.Column("plugin_id", sa.String(length=128), nullable=False),
        sa.Column("phase", sa.String(length=16), nullable=False),
        sa.Column("membership_before", sa.Boolean(), nullable=False),
        sa.Column("membership_target", sa.Boolean(), nullable=True),
        sa.Column("identity_before_revision", sa.Integer(), nullable=True),
        sa.Column("identity_target_revision", sa.Integer(), nullable=True),
        sa.Column("package_existed", sa.Boolean(), nullable=False),
        sa.Column("persistent_backup_existed", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.String(length=40), nullable=False),
        sa.Column("updated_at", sa.String(length=40), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default="1"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "transaction_id",
            name="uq_plugininstallation_transaction_id",
        ),
        sa.CheckConstraint("plugin_id <> ''", name="ck_plugininstallation_plugin_id"),
        sa.CheckConstraint("phase <> ''", name="ck_plugininstallation_phase"),
    )
    op.create_index(
        "ix_plugininstallation_plugin_id",
        "plugininstallation",
        ["plugin_id"],
    )
    op.create_index(
        "ix_plugininstallation_phase",
        "plugininstallation",
        ["phase"],
    )


def downgrade() -> None:
    """删除插件安装事务状态表。"""
    inspector = sa.inspect(op.get_bind())
    if "plugininstallation" not in inspector.get_table_names():
        return
    op.drop_index("ix_plugininstallation_phase", table_name="plugininstallation")
    op.drop_index("ix_plugininstallation_plugin_id", table_name="plugininstallation")
    op.drop_table("plugininstallation")
