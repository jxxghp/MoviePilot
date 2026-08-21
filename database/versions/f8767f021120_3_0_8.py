"""3.0.8
新增插件实例配置表、用户第三方身份绑定表与服务实例配置表

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

_SERVICECONFIG_DEFAULT_TARGET_COLUMN = "is_default_target"
_SERVICECONFIG_DEFAULT_TARGET_INDEX = "ux_serviceconfig_default_target"
# 内建类型的提供方保留值，与 app.db.models.serviceconfig.BUILTIN_PROVIDER 同值。
# 迁移是历史快照，常量自带副本而不是 import 模型：跟着当前代码一起演进会让旧库
# 重放出与当初不同的取值。
_BUILTIN_PROVIDER = "host:builtin"


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
    _create_serviceconfig_table()


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


def _default_target_predicate() -> sa.sql.ClauseElement:
    """返回服务实例默认调用目标条件索引的谓词表达式。"""
    return sa.column(_SERVICECONFIG_DEFAULT_TARGET_COLUMN, sa.Boolean()).is_(True)


def _create_serviceconfig_table() -> None:
    """建立服务实例配置表及其唯一约束与索引。

    ``UniqueConstraint("capability", "type", "name")`` 不含 ``provider``：跨提供方
    生效才能保证扩展换标识重装后同名配置不会变成两条。唯一约束随建表一并声明，
    SQLite 不支持事后 ALTER TABLE 添加约束。

    该约束自带的索引已按最左前缀覆盖「按 capability 列出」与「按 (capability, type)
    取用」两类查询，因此另建的索引只有两条：``ix_serviceconfig_provider`` 按提供方
    筛出「提供方已消失」的配置；``ux_serviceconfig_default_target`` 是条件唯一索引，
    表达「每族至多一个默认调用目标」——只索引置位的行，同一族因而可以有任意多行未
    置位、至多一行置位。部分索引的谓词是方言特性，SQLite 与 PostgreSQL 各给一份，
    两边分别渲染成 ``IS 1`` 与 ``IS true``。
    """
    if not _has_table("serviceconfig"):
        dialect_name = op.get_bind().dialect.name
        op.create_table(
            "serviceconfig",
            _id_column(dialect_name),
            sa.Column("capability", sa.String(), nullable=False),
            sa.Column("type", sa.String(), nullable=False),
            sa.Column("name", sa.String(), nullable=False),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("config", sa.JSON(), nullable=True),
            sa.Column(
                _SERVICECONFIG_DEFAULT_TARGET_COLUMN,
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
            sa.Column(
                "provider",
                sa.String(),
                nullable=False,
                server_default=_BUILTIN_PROVIDER,
            ),
            sa.UniqueConstraint(
                "capability", "type", "name", name="ux_serviceconfig_capability_type_name"
            ),
        )
    if not _has_index("serviceconfig", "ix_serviceconfig_provider"):
        op.create_index("ix_serviceconfig_provider", "serviceconfig", ["provider"])
    if not _has_index("serviceconfig", _SERVICECONFIG_DEFAULT_TARGET_INDEX):
        op.create_index(
            _SERVICECONFIG_DEFAULT_TARGET_INDEX,
            "serviceconfig",
            ["capability"],
            unique=True,
            sqlite_where=_default_target_predicate(),
            postgresql_where=_default_target_predicate(),
        )


def downgrade() -> None:
    """删除插件实例配置表、用户第三方身份绑定表与服务实例配置表，唯一约束与外键随建表内联声明，随表一并删除。"""
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

    if _has_table("serviceconfig"):
        if _has_index("serviceconfig", _SERVICECONFIG_DEFAULT_TARGET_INDEX):
            op.drop_index(_SERVICECONFIG_DEFAULT_TARGET_INDEX, table_name="serviceconfig")
        if _has_index("serviceconfig", "ix_serviceconfig_provider"):
            op.drop_index("ix_serviceconfig_provider", table_name="serviceconfig")
        op.drop_table("serviceconfig")
