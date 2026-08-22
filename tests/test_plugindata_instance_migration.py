"""插件数据表加实例维度的迁移行为：58e7ce2cd0fd_3_0_9。

覆盖列与索引的幂等升级、索引替换、降级还原，以及「存在分身实例数据时拒绝降级」
这条防丢数据的硬约束。
"""

import importlib

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.db.models.plugindata import PluginData
from app.runtime.extensions.contract.instance import DEFAULT_INSTANCE_ID


MIGRATION = "database.versions.58e7ce2cd0fd_3_0_9"


def _bind_migration(monkeypatch, connection):
    """把迁移绑定到隔离数据库连接。"""
    migration = importlib.import_module(MIGRATION)
    context = MigrationContext.configure(connection)
    monkeypatch.setattr(migration, "op", Operations(context))
    return migration


def _legacy_plugindata_table(metadata: sa.MetaData) -> sa.Table:
    """构造迁移前（无 instance_id 列）的插件数据表。"""
    table = sa.Table(
        "plugindata",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("plugin_id", sa.String(), nullable=False),
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("value", sa.JSON()),
    )
    return table


def test_migration_adds_instance_column_and_swaps_index(monkeypatch) -> None:
    """升级需新增 instance_id 列，并把两列索引换成三列索引，重复升级保持幂等。"""
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        metadata = sa.MetaData()
        table = _legacy_plugindata_table(metadata)
        metadata.create_all(connection)
        sa.Index("ix_plugindata_plugin_id_key", table.c.plugin_id, table.c.key).create(connection)
        connection.execute(table.insert(), [{"plugin_id": "PluginA", "key": "k1", "value": {"v": 1}}])

        migration = _bind_migration(monkeypatch, connection)
        migration.upgrade()
        migration.upgrade()

        inspector = sa.inspect(connection)
        columns = {column["name"] for column in inspector.get_columns("plugindata")}
        assert columns == {column.name for column in PluginData.__table__.columns}

        indexes = {
            index["name"]: tuple(index["column_names"])
            for index in inspector.get_indexes("plugindata")
        }
        assert indexes.get("ix_plugindata_plugin_id_instance_id_key") == (
            "plugin_id", "instance_id", "key",
        )
        assert "ix_plugindata_plugin_id_key" not in indexes

        rows = connection.execute(
            sa.text("select plugin_id, instance_id, key from plugindata")
        ).fetchall()
        assert rows == [("PluginA", DEFAULT_INSTANCE_ID, "k1")]


def test_migration_downgrade_restores_legacy_index(monkeypatch) -> None:
    """降级需删除 instance_id 列，并把三列索引换回两列索引。"""
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        metadata = sa.MetaData()
        _legacy_plugindata_table(metadata)
        metadata.create_all(connection)

        migration = _bind_migration(monkeypatch, connection)
        migration.upgrade()
        migration.downgrade()

        inspector = sa.inspect(connection)
        columns = {column["name"] for column in inspector.get_columns("plugindata")}
        assert "instance_id" not in columns

        indexes = {
            index["name"]: tuple(index["column_names"])
            for index in inspector.get_indexes("plugindata")
        }
        assert indexes.get("ix_plugindata_plugin_id_key") == ("plugin_id", "key")
        assert "ix_plugindata_plugin_id_instance_id_key" not in indexes


def test_migration_downgrade_rejects_when_non_default_instance_rows_exist(monkeypatch) -> None:
    """存在分身实例的数据行时拒绝降级，避免默默丢失这些行。"""
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        metadata = sa.MetaData()
        _legacy_plugindata_table(metadata)
        metadata.create_all(connection)

        migration = _bind_migration(monkeypatch, connection)
        migration.upgrade()

        connection.execute(sa.text(
            "insert into plugindata (plugin_id, instance_id, key, value) "
            "values ('PluginA', 'clone-a', 'k1', '1')"
        ))

        with pytest.raises(RuntimeError, match="非默认实例"):
            migration.downgrade()

        # 拒绝后原列与索引应保持不变，未发生部分回退
        inspector = sa.inspect(connection)
        columns = {column["name"] for column in inspector.get_columns("plugindata")}
        assert "instance_id" in columns
