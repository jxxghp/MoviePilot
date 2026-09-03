"""插件实例默认调用目标标记列与条件唯一索引 Alembic 迁移测试。"""

from __future__ import annotations

import importlib
from datetime import datetime, timezone

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.exc import IntegrityError

from app.db.models.plugininstance import PluginInstanceDescriptor

MIGRATION_MODULE = "database.versions.e0e68cbd5756_3_0_31"


def _bind_migration(monkeypatch, connection):
    """把迁移绑定到隔离数据库连接。"""
    migration = importlib.import_module(MIGRATION_MODULE)
    context = MigrationContext.configure(connection)
    monkeypatch.setattr(migration, "op", Operations(context))
    return migration


def _create_legacy_table(connection: sa.engine.Connection) -> None:
    """建出加列前的表结构，模拟迁移前的存量数据库。"""
    now = datetime.now(timezone.utc).isoformat()
    table = sa.Table(
        "plugininstance",
        sa.MetaData(),
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("instance_id", sa.String(length=128), nullable=False),
        sa.Column("source_plugin_id", sa.String(length=128), nullable=False),
        sa.Column("plugin_name", sa.String(length=255)),
        sa.Column("plugin_desc", sa.String(length=255)),
        sa.Column("plugin_icon", sa.String(length=255)),
        sa.Column("mode", sa.String(length=16), nullable=False, server_default="virtual"),
        sa.Column("plugin_version", sa.String(length=64)),
        sa.Column("follow_current_version", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("log_level", sa.String(length=16)),
        sa.Column("log_expires_at", sa.String(length=40)),
        sa.Column("created_at", sa.String(length=40), nullable=False),
        sa.Column("updated_at", sa.String(length=40), nullable=False),
    )
    table.create(connection)
    connection.execute(
        table.insert().values(
            instance_id="DemoPluginWork",
            source_plugin_id="DemoPlugin",
            mode="virtual",
            follow_current_version=True,
            created_at=now,
            updated_at=now,
        )
    )


def test_default_target_migration_adds_column_and_keeps_existing_rows(monkeypatch) -> None:
    """新增列必须可空默认为假，且不得影响已有行；重复升级与完整回滚都要幂等。"""
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        _create_legacy_table(connection)
        migration = _bind_migration(monkeypatch, connection)

        migration.upgrade()
        migration.upgrade()

        columns = {
            column["name"]: column
            for column in sa.inspect(connection).get_columns("plugininstance")
        }
        # 与该迁移自身的落点比对，而不是与持续演进的当前模型比对：后续迁移
        # 还会继续给该表加列，本断言不应该随之跟着变红。
        assert columns.keys() == {
            "id",
            "instance_id",
            "source_plugin_id",
            "plugin_name",
            "plugin_desc",
            "plugin_icon",
            "mode",
            "plugin_version",
            "follow_current_version",
            "log_level",
            "log_expires_at",
            "is_default_target",
            "created_at",
            "updated_at",
        }
        assert columns["is_default_target"]["nullable"] is False

        table = sa.Table("plugininstance", sa.MetaData(), autoload_with=connection)
        row = connection.execute(sa.select(table)).mappings().one()
        assert row["instance_id"] == "DemoPluginWork"
        assert bool(row["is_default_target"]) is False

        migration.downgrade()
        remaining = {
            column["name"] for column in sa.inspect(connection).get_columns("plugininstance")
        }
        assert "is_default_target" not in remaining
        remaining_indexes = {
            index["name"] for index in sa.inspect(connection).get_indexes("plugininstance")
        }
        assert "ux_plugininstance_default_target" not in remaining_indexes

        migration.upgrade()
        restored = {
            column["name"] for column in sa.inspect(connection).get_columns("plugininstance")
        }
        assert "is_default_target" in restored


def test_default_target_migration_accepts_fresh_current_schema(monkeypatch) -> None:
    """create_all 已建当前表时重复升级不得因列或索引已存在而报错。"""
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        PluginInstanceDescriptor.__table__.create(connection)
        migration = _bind_migration(monkeypatch, connection)

        migration.upgrade()
        migration.upgrade()

        assert {
            column["name"]
            for column in sa.inspect(connection).get_columns("plugininstance")
        } == {column.name for column in PluginInstanceDescriptor.__table__.columns}
        assert "ux_plugininstance_default_target" in {
            index["name"] for index in sa.inspect(connection).get_indexes("plugininstance")
        }


def test_default_target_migration_index_rejects_a_second_default_target(monkeypatch) -> None:
    """迁移建出的条件唯一索引必须在真实数据库连接上拒绝第二条置位。"""
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        _create_legacy_table(connection)
        migration = _bind_migration(monkeypatch, connection)
        migration.upgrade()

        table = sa.Table("plugininstance", sa.MetaData(), autoload_with=connection)
        now = datetime.now(timezone.utc).isoformat()
        connection.execute(
            table.insert().values(
                instance_id="DemoPlugin",
                source_plugin_id="DemoPlugin",
                mode="host",
                follow_current_version=True,
                is_default_target=True,
                created_at=now,
                updated_at=now,
            )
        )

        with pytest.raises(IntegrityError):
            connection.execute(
                table.update()
                .where(table.c.instance_id == "DemoPluginWork")
                .values(is_default_target=True)
            )


def test_default_target_index_is_partial_in_both_dialects() -> None:
    """模型（``create_all`` 路径）建出的索引在两种方言下都必须带谓词。

    本仓测试库是 SQLite，PostgreSQL 分支只能靠编译期 DDL 证明：谓词整个丢失会
    退化成「每个源插件只能有一行实例」，把插件分身整个锁死。
    """
    index = next(
        item for item in PluginInstanceDescriptor.__table__.indexes
        if item.name == "ux_plugininstance_default_target"
    )
    ddl = sa.schema.CreateIndex(index)

    assert str(ddl.compile(dialect=sqlite.dialect())).strip() == (
        "CREATE UNIQUE INDEX ux_plugininstance_default_target "
        "ON plugininstance (source_plugin_id) WHERE is_default_target IS 1"
    )
    assert str(ddl.compile(dialect=postgresql.dialect())).strip() == (
        "CREATE UNIQUE INDEX ux_plugininstance_default_target "
        "ON plugininstance (source_plugin_id) WHERE is_default_target IS true"
    )
