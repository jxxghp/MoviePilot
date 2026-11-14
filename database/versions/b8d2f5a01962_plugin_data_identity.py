"""
合并插件同键旧重复数据，并为安装补偿保留本体启用位快照。
"""

import sqlalchemy as sa
from alembic import op

revision = "b8d2f5a01962"
down_revision = "e6a4c9f20813"
branch_labels = None
depends_on = None


_INDEX = "ix_plugindata_plugin_id_key"


def upgrade() -> None:
    """
    保留原同步读取最先可见的最低ID记录，去除其余同插件同键重复行后创建唯一索引。
    """
    connection = op.get_bind()
    table = sa.table("plugindata", sa.column("id", sa.Integer), sa.column("plugin_id", sa.String),
                     sa.column("key", sa.String))
    keep_ids = sa.select(sa.func.min(table.c.id)).group_by(table.c.plugin_id, table.c.key)
    connection.execute(table.delete().where(table.c.id.not_in(keep_ids)))
    indexes = {index["name"]: index for index in sa.inspect(connection).get_indexes("plugindata")}
    if _INDEX in indexes and not indexes[_INDEX].get("unique"):
        op.drop_index(_INDEX, table_name="plugindata")
    if _INDEX not in indexes or not indexes[_INDEX].get("unique"):
        op.create_index(_INDEX, "plugindata", ["plugin_id", "key"], unique=True)
    if "plugininstallation" in sa.inspect(connection).get_table_names():
        columns = {column["name"] for column in sa.inspect(connection).get_columns("plugininstallation")}
        if "host_enabled_before" not in columns:
            # 旧journal无法推断被提前覆盖的启用位，保留NULL表示需人工复核。
            op.add_column("plugininstallation", sa.Column("host_enabled_before", sa.Boolean, nullable=True))


def downgrade() -> None:
    """
    恢复非唯一索引；升级已合并的旧重复值不重新制造。
    """
    connection = op.get_bind()
    if "plugininstallation" in sa.inspect(connection).get_table_names():
        columns = {column["name"] for column in sa.inspect(connection).get_columns("plugininstallation")}
        if "host_enabled_before" in columns:
            op.drop_column("plugininstallation", "host_enabled_before")
    op.drop_index(_INDEX, table_name="plugindata")
    op.create_index(_INDEX, "plugindata", ["plugin_id", "key"], unique=False)
