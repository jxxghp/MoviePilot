"""3.0.8
新增插件实例配置表与用户第三方身份绑定表

Revision ID: f8767f021120
Revises: 73370ce9bab7
Create Date: 2026-08-19
"""

from alembic import op
import sqlalchemy as sa


revision = "f8767f021120"
down_revision = "73370ce9bab7"
branch_labels = None
depends_on = None


def _inspector() -> sa.Inspector:
    """返回使用当前迁移连接的数据库检查器。"""
    return sa.inspect(op.get_bind())


def _has_table(table_name: str) -> bool:
    """检查表是否存在。"""
    return table_name in _inspector().get_table_names()


def _has_index(table_name: str, index_name: str) -> bool:
    """检查表索引或唯一约束是否存在。"""
    if not _has_table(table_name):
        return False
    inspector = _inspector()
    names = {index.get("name") for index in inspector.get_indexes(table_name)}
    names.update(
        constraint.get("name")
        for constraint in inspector.get_unique_constraints(table_name)
    )
    return index_name in names


def _id_column(dialect_name: str) -> sa.Column:
    """生成与当前 ORM 一致的自增主键定义。"""
    if dialect_name == "postgresql":
        return sa.Column(
            "id",
            sa.Integer(),
            sa.Identity(start=1, cycle=True),
            primary_key=True,
        )
    return sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True)


def upgrade() -> None:
    """建立插件实例配置表及其唯一约束与索引。

    唯一约束随建表一并声明：SQLite 不支持事后 ALTER TABLE 添加约束，只能在
    CREATE TABLE 时一次性带上；这张表没有需要兼容的历史结构，不必再额外处理
    「表已存在但约束缺失」的分支。
    """
    if not _has_table("pluginconfig"):
        dialect_name = op.get_bind().dialect.name
        op.create_table(
            "pluginconfig",
            _id_column(dialect_name),
            sa.Column("plugin_id", sa.String(), nullable=False),
            sa.Column("instance_id", sa.String(), nullable=False, server_default="default"),
            sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("log_level", sa.String(), nullable=True),
            sa.Column("log_expires_at", sa.DateTime(), nullable=True),
            sa.Column("config_data", sa.JSON(), nullable=True),
            sa.Column("plugin_version", sa.String(), nullable=True),
            sa.Column("follow_default_version", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("created_at", sa.String(), nullable=True),
            sa.Column("updated_at", sa.String(), nullable=True),
            sa.UniqueConstraint("plugin_id", "instance_id", name="ux_pluginconfig_plugin_instance"),
        )
    if not _has_index("pluginconfig", "ix_pluginconfig_plugin_id"):
        op.create_index(
            "ix_pluginconfig_plugin_id",
            "pluginconfig",
            ["plugin_id"],
        )
    if not _has_index("pluginconfig", "ix_pluginconfig_plugin_id_plugin_version"):
        op.create_index(
            "ix_pluginconfig_plugin_id_plugin_version",
            "pluginconfig",
            ["plugin_id", "plugin_version"],
        )

    _create_useridentity_table()


def _create_useridentity_table() -> None:
    """建立用户第三方身份绑定表及其唯一约束、外键与索引。

    ``user_id`` 外键带 ``ON DELETE CASCADE``：用户删除时数据库层级联删除其全部
    身份绑定行。``UniqueConstraint("provider", "external_id")`` 禁止同一第三方
    身份绑定到多个本项目用户，不对 ``(user_id, provider)`` 设唯一约束——同一用户
    允许绑定同一 provider 族下的多个实例（如两台媒体服务器）。唯一约束与外键随建表
    一并声明：SQLite 不支持事后 ALTER TABLE 添加约束，只能在 CREATE TABLE 时一次性带上。
    """
    if not _has_table("useridentity"):
        dialect_name = op.get_bind().dialect.name
        op.create_table(
            "useridentity",
            _id_column(dialect_name),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("provider", sa.String(), nullable=False),
            sa.Column("external_id", sa.String(), nullable=False),
            sa.Column("display_name", sa.String(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(
                ["user_id"], ["user.id"],
                name="fk_useridentity_user_id",
                ondelete="CASCADE",
            ),
            sa.UniqueConstraint(
                "provider", "external_id", name="ux_useridentity_provider_external_id"
            ),
        )
    if not _has_index("useridentity", "ix_useridentity_user_id"):
        op.create_index("ix_useridentity_user_id", "useridentity", ["user_id"])


def downgrade() -> None:
    """删除插件实例配置表与用户第三方身份绑定表，唯一约束与外键随建表内联声明，随表一并删除。"""
    if _has_table("pluginconfig"):
        if _has_index("pluginconfig", "ix_pluginconfig_plugin_id_plugin_version"):
            op.drop_index("ix_pluginconfig_plugin_id_plugin_version", table_name="pluginconfig")
        if _has_index("pluginconfig", "ix_pluginconfig_plugin_id"):
            op.drop_index("ix_pluginconfig_plugin_id", table_name="pluginconfig")
        op.drop_table("pluginconfig")

    if _has_table("useridentity"):
        if _has_index("useridentity", "ix_useridentity_user_id"):
            op.drop_index("ix_useridentity_user_id", table_name="useridentity")
        op.drop_table("useridentity")
