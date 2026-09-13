"""插件实例启用位 Alembic 迁移测试。"""

from __future__ import annotations

import importlib
import json
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.db.models.plugininstance import PluginInstance

MIGRATION_MODULE = "database.versions.b7d1e4a9c206_3_0_38"


def _bind_migration(monkeypatch, connection):
    """把迁移绑定到隔离数据库连接。"""
    migration = importlib.import_module(MIGRATION_MODULE)
    context = MigrationContext.configure(connection)
    monkeypatch.setattr(migration, "op", Operations(context))
    return migration


def _create_legacy_schema(connection: sa.engine.Connection, *, installed=None) -> sa.Table:
    """建出加列前的实例表与系统设置表，模拟迁移前的存量数据库。"""
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
        sa.Column("is_default_target", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("log_level", sa.String(length=16)),
        sa.Column("log_expires_at", sa.String(length=40)),
        sa.Column("config_data", sa.JSON()),
        sa.Column("created_at", sa.String(length=40), nullable=False),
        sa.Column("updated_at", sa.String(length=40), nullable=False),
    )
    table.create(connection)
    connection.execute(
        table.insert().values(
            instance_id="DemoPluginWork",
            source_plugin_id="DemoPlugin",
            created_at=now,
            updated_at=now,
        )
    )
    systemconfig = sa.Table(
        "systemconfig",
        sa.MetaData(),
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("key", sa.String(length=255)),
        sa.Column("value", sa.Text()),
    )
    systemconfig.create(connection)
    if installed is not None:
        connection.execute(
            systemconfig.insert().values(
                key="UserInstalledPlugins",
                value=json.dumps(installed),
            )
        )
    return table


def test_enablement_migration_enables_existing_rows_and_backfills_hosts(monkeypatch) -> None:
    """存量分身一律置真，安装清单里的插件补出启用中的本体行；重复升级保持幂等。

    列建出来默认为假而不回填，升级后全部插件都会变成「装着但永不加载」——这一列
    同时是本体的装载判据，回填因此是迁移的正文而不是附带步骤。
    """
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        _create_legacy_schema(connection, installed=["DemoPlugin", "OtherPlugin"])
        migration = _bind_migration(monkeypatch, connection)

        migration.upgrade()
        migration.upgrade()

        table = sa.Table("plugininstance", sa.MetaData(), autoload_with=connection)
        rows = {
            row["instance_id"]: row
            for row in connection.execute(sa.select(table)).mappings().all()
        }
        assert set(rows) == {"DemoPluginWork", "DemoPlugin", "OtherPlugin"}
        assert all(bool(row["is_enabled"]) for row in rows.values())
        # 本体行按自身 ID 建出，两列身份相等
        assert rows["OtherPlugin"]["source_plugin_id"] == "OtherPlugin"
        # 已有的分身行没有被重复插入，也没有被改写归属
        assert rows["DemoPluginWork"]["source_plugin_id"] == "DemoPlugin"

        migration.downgrade()
        assert "is_enabled" not in {
            column["name"] for column in sa.inspect(connection).get_columns("plugininstance")
        }


def test_enablement_migration_tolerates_a_missing_installed_list(monkeypatch) -> None:
    """安装清单不存在时只补启用位，不因读不到清单而中断升级。"""
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        _create_legacy_schema(connection, installed=None)
        migration = _bind_migration(monkeypatch, connection)

        migration.upgrade()

        table = sa.Table("plugininstance", sa.MetaData(), autoload_with=connection)
        rows = connection.execute(sa.select(table)).mappings().all()
        assert [row["instance_id"] for row in rows] == ["DemoPluginWork"]
        assert bool(rows[0]["is_enabled"]) is True


def test_enablement_migration_ignores_a_malformed_installed_list(monkeypatch) -> None:
    """安装清单不是字符串数组时按空清单处理，不能让升级崩在一条脏数据上。"""
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        _create_legacy_schema(connection, installed=None)
        systemconfig = sa.Table("systemconfig", sa.MetaData(), autoload_with=connection)
        connection.execute(
            systemconfig.insert().values(key="UserInstalledPlugins", value="not-json")
        )
        migration = _bind_migration(monkeypatch, connection)

        migration.upgrade()

        table = sa.Table("plugininstance", sa.MetaData(), autoload_with=connection)
        assert [
            row["instance_id"]
            for row in connection.execute(sa.select(table)).mappings().all()
        ] == ["DemoPluginWork"]


def test_enablement_migration_accepts_fresh_current_schema(monkeypatch) -> None:
    """create_all 已建当前表时重复升级不得因列已存在而报错。"""
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        PluginInstance.__table__.create(connection)
        migration = _bind_migration(monkeypatch, connection)

        migration.upgrade()
        migration.upgrade()

        assert {
            column["name"]
            for column in sa.inspect(connection).get_columns("plugininstance")
        } == {column.name for column in PluginInstance.__table__.columns}
