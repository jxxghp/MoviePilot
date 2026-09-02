"""插件实例描述符表 Alembic 迁移测试。"""

from __future__ import annotations

import importlib

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.db.models.plugininstance import PluginInstanceDescriptor
from app.db.models.systemconfig import SystemConfig

MIGRATION_MODULE = "database.versions.281965691a20_3_0_24"


def _bind_migration(monkeypatch, connection):
    """把迁移绑定到隔离数据库连接。"""
    migration = importlib.import_module(MIGRATION_MODULE)
    context = MigrationContext.configure(connection)
    monkeypatch.setattr(migration, "op", Operations(context))
    return migration


def _seed_legacy_key(connection: sa.engine.Connection, value) -> None:
    """写入旧 systemconfig 单键，模拟迁移前的实例描述存量数据。"""
    connection.execute(
        sa.insert(SystemConfig.__table__).values(key="PluginInstances", value=value)
    )


def test_plugin_instance_migration_migrates_legacy_dict_payload_and_keeps_source_key(
    monkeypatch,
) -> None:
    """字典载荷应逐条搬入新表，原 systemconfig 键保留不删，且可重复升级与完整回滚。"""
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        SystemConfig.__table__.create(connection)
        _seed_legacy_key(
            connection,
            {
                "DemoPluginWork": {
                    "instance_id": "DemoPluginWork",
                    "source_plugin_id": "DemoPlugin",
                    "plugin_name": "工作实例",
                    "follow_current_version": False,
                    "plugin_version": "1.2.0",
                },
            },
        )
        migration = _bind_migration(monkeypatch, connection)

        migration.upgrade()
        migration.upgrade()

        inspector = sa.inspect(connection)
        assert "plugininstance" in inspector.get_table_names()
        columns = {column["name"] for column in inspector.get_columns("plugininstance")}
        assert columns == {
            column.name for column in PluginInstanceDescriptor.__table__.columns
        }
        unique_constraints = {
            constraint["name"]: tuple(constraint["column_names"])
            for constraint in inspector.get_unique_constraints("plugininstance")
        }
        assert unique_constraints["uq_plugininstance_instance_id"] == ("instance_id",)
        check_constraints = {
            constraint["name"]
            for constraint in inspector.get_check_constraints("plugininstance")
        }
        assert "ck_plugininstance_mode" in check_constraints
        indexes = {index["name"] for index in inspector.get_indexes("plugininstance")}
        assert "ix_plugininstance_source_plugin_id" in indexes

        table = sa.Table("plugininstance", sa.MetaData(), autoload_with=connection)
        rows = connection.execute(sa.select(table)).mappings().all()
        assert len(rows) == 1
        row = rows[0]
        assert row["instance_id"] == "DemoPluginWork"
        assert row["source_plugin_id"] == "DemoPlugin"
        assert row["plugin_name"] == "工作实例"
        assert row["mode"] == "virtual"
        assert row["plugin_version"] == "1.2.0"
        assert row["follow_current_version"] is False

        legacy_row = connection.execute(
            sa.select(SystemConfig.value).where(SystemConfig.key == "PluginInstances")
        ).scalar_one()
        assert legacy_row == {
            "DemoPluginWork": {
                "instance_id": "DemoPluginWork",
                "source_plugin_id": "DemoPlugin",
                "plugin_name": "工作实例",
                "follow_current_version": False,
                "plugin_version": "1.2.0",
            },
        }

        migration.downgrade()
        assert "plugininstance" not in sa.inspect(connection).get_table_names()
        assert connection.execute(
            sa.select(SystemConfig.value).where(SystemConfig.key == "PluginInstances")
        ).scalar_one() == legacy_row

        migration.upgrade()
        restored = sa.Table("plugininstance", sa.MetaData(), autoload_with=connection)
        restored_rows = connection.execute(sa.select(restored)).mappings().all()
        assert len(restored_rows) == 1
        assert restored_rows[0]["instance_id"] == "DemoPluginWork"


def test_plugin_instance_migration_migrates_legacy_list_payload(monkeypatch) -> None:
    """历史列表载荷同样应逐条搬入新表。"""
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        SystemConfig.__table__.create(connection)
        _seed_legacy_key(
            connection,
            [
                {"instance_id": "DemoPluginWork", "source_plugin_id": "DemoPlugin"},
                {"instance_id": "DemoPluginBackup", "source_plugin_id": "DemoPlugin"},
            ],
        )
        migration = _bind_migration(monkeypatch, connection)
        migration.upgrade()

        table = sa.Table("plugininstance", sa.MetaData(), autoload_with=connection)
        instance_ids = {
            row["instance_id"]
            for row in connection.execute(sa.select(table)).mappings().all()
        }
        assert instance_ids == {"DemoPluginWork", "DemoPluginBackup"}


def test_plugin_instance_migration_skips_malformed_legacy_entries(monkeypatch) -> None:
    """缺失必填字段或非字典条目必须被跳过，不得中断迁移。"""
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        SystemConfig.__table__.create(connection)
        _seed_legacy_key(
            connection,
            [
                {"instance_id": "MissingSource"},
                {"source_plugin_id": "DemoPlugin"},
                "not-a-dict",
                {"instance_id": "DemoPluginWork", "source_plugin_id": "DemoPlugin"},
            ],
        )
        migration = _bind_migration(monkeypatch, connection)
        migration.upgrade()

        table = sa.Table("plugininstance", sa.MetaData(), autoload_with=connection)
        rows = connection.execute(sa.select(table)).mappings().all()
        assert [row["instance_id"] for row in rows] == ["DemoPluginWork"]


def test_plugin_instance_migration_without_legacy_key_creates_empty_table(
    monkeypatch,
) -> None:
    """旧键缺失或为空时应正常建表，不产生任何数据行。"""
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        SystemConfig.__table__.create(connection)
        migration = _bind_migration(monkeypatch, connection)
        migration.upgrade()

        assert "plugininstance" in sa.inspect(connection).get_table_names()
        table = sa.Table("plugininstance", sa.MetaData(), autoload_with=connection)
        assert connection.execute(sa.select(table)).mappings().all() == []


def test_plugin_instance_migration_accepts_fresh_current_schema(monkeypatch) -> None:
    """create_all 已建当前表时重复升级不得创建冲突对象。"""
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        SystemConfig.__table__.create(connection)
        PluginInstanceDescriptor.__table__.create(connection)
        migration = _bind_migration(monkeypatch, connection)

        migration.upgrade()
        migration.upgrade()

        assert {
            column["name"]
            for column in sa.inspect(connection).get_columns("plugininstance")
        } == {column.name for column in PluginInstanceDescriptor.__table__.columns}
