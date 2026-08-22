"""3.0.9
插件数据表加实例维度

Revision ID: 58e7ce2cd0fd
Revises: f8767f021120
Create Date: 2026-08-19
"""

from alembic import op
import sqlalchemy as sa

from app.runtime.extensions.contract.instance import DEFAULT_INSTANCE_ID


revision = "58e7ce2cd0fd"
down_revision = "f8767f021120"
branch_labels = None
depends_on = None

_OLD_INDEX = "ix_plugindata_plugin_id_key"
_NEW_INDEX = "ix_plugindata_plugin_id_instance_id_key"


def _inspector() -> sa.Inspector:
    """返回使用当前迁移连接的数据库检查器。"""
    return sa.inspect(op.get_bind())


def _has_table(table_name: str) -> bool:
    """检查表是否存在。"""
    return table_name in _inspector().get_table_names()


def _has_column(table_name: str, column_name: str) -> bool:
    """检查数据表是否已存在指定字段。"""
    if not _has_table(table_name):
        return False
    return any(column["name"] == column_name for column in _inspector().get_columns(table_name))


def _has_index(table_name: str, index_name: str) -> bool:
    """检查表索引是否存在。"""
    if not _has_table(table_name):
        return False
    return any(index.get("name") == index_name for index in _inspector().get_indexes(table_name))


def _has_non_default_instance_rows() -> bool:
    """检查插件数据表中是否存在非默认实例的数据行。"""
    plugindata = sa.table("plugindata", sa.column("instance_id", sa.String()))
    connection = op.get_bind()
    row = connection.execute(
        sa.select(plugindata.c.instance_id)
        .where(plugindata.c.instance_id != DEFAULT_INSTANCE_ID)
        .limit(1)
    ).first()
    return row is not None


def upgrade() -> None:
    """给插件数据表加实例标识列，并把 (plugin_id, key) 索引换成 (plugin_id, instance_id, key)。"""
    if not _has_table("plugindata"):
        return
    if not _has_column("plugindata", "instance_id"):
        op.add_column(
            "plugindata",
            sa.Column("instance_id", sa.String(), nullable=False, server_default=DEFAULT_INSTANCE_ID),
        )
    if _has_index("plugindata", _OLD_INDEX):
        op.drop_index(_OLD_INDEX, table_name="plugindata")
    if not _has_index("plugindata", _NEW_INDEX):
        op.create_index(_NEW_INDEX, "plugindata", ["plugin_id", "instance_id", "key"])


def downgrade() -> None:
    """
    回退插件数据表的实例维度。

    存在非默认实例的数据行时拒绝降级：这些行只在实例维度下才有归属，删列会
    直接丢失分身实例的数据，而不是退化为默认实例的数据。
    """
    if not _has_table("plugindata"):
        return
    if _has_column("plugindata", "instance_id") and _has_non_default_instance_rows():
        raise RuntimeError("plugindata 存在非默认实例数据，拒绝降级以避免丢失分身实例数据")
    if _has_index("plugindata", _NEW_INDEX):
        op.drop_index(_NEW_INDEX, table_name="plugindata")
    if not _has_index("plugindata", _OLD_INDEX):
        op.create_index(_OLD_INDEX, "plugindata", ["plugin_id", "key"])
    if _has_column("plugindata", "instance_id"):
        op.drop_column("plugindata", "instance_id")
