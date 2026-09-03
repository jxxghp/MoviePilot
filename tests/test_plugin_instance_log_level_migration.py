"""插件实例日志等级覆盖列 Alembic 迁移测试。"""

from __future__ import annotations

import importlib
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.db.models.plugininstance import PluginInstance

MIGRATION_MODULE = "database.versions.487f7e681955_3_0_30"


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


def test_plugin_instance_log_level_migration_adds_columns_and_keeps_existing_rows(
    monkeypatch,
) -> None:
    """新增列必须可空，且不得影响已有行；重复升级与完整回滚都要幂等。"""
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
            "created_at",
            "updated_at",
        }
        assert columns["log_level"]["nullable"] is True
        assert columns["log_expires_at"]["nullable"] is True

        table = sa.Table("plugininstance", sa.MetaData(), autoload_with=connection)
        row = connection.execute(sa.select(table)).mappings().one()
        assert row["instance_id"] == "DemoPluginWork"
        assert row["log_level"] is None
        assert row["log_expires_at"] is None

        migration.downgrade()
        remaining = {
            column["name"] for column in sa.inspect(connection).get_columns("plugininstance")
        }
        assert "log_level" not in remaining
        assert "log_expires_at" not in remaining

        migration.upgrade()
        restored = {
            column["name"] for column in sa.inspect(connection).get_columns("plugininstance")
        }
        assert {"log_level", "log_expires_at"}.issubset(restored)


def test_plugin_instance_log_level_migration_accepts_fresh_current_schema(
    monkeypatch,
) -> None:
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
