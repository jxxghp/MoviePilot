"""插件配置迁入插件实例表的 Alembic 迁移测试。"""

from __future__ import annotations

import importlib
import json

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

MIGRATION_MODULE = "database.versions.c4e1a7b9d2f6_3_0_35"


def _bind_migration(monkeypatch, connection):
    """把迁移绑定到隔离数据库连接。"""
    migration = importlib.import_module(MIGRATION_MODULE)
    context = MigrationContext.configure(connection)
    monkeypatch.setattr(migration, "op", Operations(context))
    return migration


def _create_legacy_schema(connection: sa.engine.Connection) -> None:
    """建出迁移前的系统设置表与实例表，并写入两类数据各若干条。

    实例表按上一条迁移 281965691a20 建出的形状，不含业务参数列。
    """
    sa.Table(
        "systemconfig",
        sa.MetaData(),
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("value", sa.JSON()),
    ).create(connection)
    connection.execute(
        sa.text("INSERT INTO systemconfig (key, value) VALUES (:k, :v)"),
        [
            {"k": "plugin.DemoPlugin", "v": json.dumps({"enable": True, "token": "keep"})},
            {"k": "plugin.DemoPluginwork", "v": json.dumps({"enable": False})},
            {"k": "UserInstalledPlugins", "v": json.dumps(["DemoPlugin", "BareInstalled"])},
        ],
    )

    sa.Table(
        "plugininstance",
        sa.MetaData(),
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("instance_id", sa.String(128), nullable=False, unique=True),
        sa.Column("source_plugin_id", sa.String(128), nullable=False),
        sa.Column("plugin_name", sa.String(255)),
        sa.Column("plugin_desc", sa.String(255)),
        sa.Column("plugin_icon", sa.String(255)),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.Column("updated_at", sa.String(40), nullable=False),
    ).create(connection)
    connection.execute(
        sa.text(
            "INSERT INTO plugininstance "
            "(instance_id, source_plugin_id, plugin_name, created_at, updated_at) "
            "VALUES (:iid, :sid, :name, 'x', 'x')"
        ),
        [{"iid": "DemoPluginwork", "sid": "DemoPlugin", "name": "分身甲"}],
    )


def _instance_rows(connection: sa.engine.Connection) -> dict:
    """读取实例表内容，兼容驱动把 JSON 列返回为字符串的情形。"""
    rows = connection.execute(
        sa.text(
            "SELECT instance_id, source_plugin_id, plugin_name, config_data "
            "FROM plugininstance"
        )
    ).fetchall()
    return {
        row[0]: {
            "source_plugin_id": row[1],
            "plugin_name": row[2],
            "config_data": json.loads(row[3]) if isinstance(row[3], str) else row[3],
        }
        for row in rows
    }


def _system_keys(connection: sa.engine.Connection) -> set:
    """读取系统设置表现存的键。"""
    return {
        row[0]
        for row in connection.execute(sa.text("SELECT key FROM systemconfig")).fetchall()
    }


def _columns(connection: sa.engine.Connection) -> set:
    """读取实例表当前列名。"""
    return {column["name"] for column in sa.inspect(connection).get_columns("plugininstance")}


def test_migration_moves_plugin_config_onto_the_instance_rows(monkeypatch):
    """插件配置搬到实例行上，原行一并删除，真·系统设置留在原地。"""
    engine = sa.create_engine("sqlite://")
    with engine.connect() as connection:
        _create_legacy_schema(connection)
        migration = _bind_migration(monkeypatch, connection)

        migration.upgrade()

        rows = _instance_rows(connection)
        assert rows["DemoPlugin"]["config_data"] == {"enable": True, "token": "keep"}
        # 本体没有实例行时按自身建出，归属就是它自己
        assert rows["DemoPlugin"]["source_plugin_id"] == "DemoPlugin"
        # 分身已有实例行，配置并到那一行上而不是另起一行
        assert rows["DemoPluginwork"]["config_data"] == {"enable": False}
        assert rows["DemoPluginwork"]["source_plugin_id"] == "DemoPlugin"
        # 同一份配置不能在两张表里各留一份，否则读取端按哪个都不对
        assert _system_keys(connection) == {"UserInstalledPlugins"}


def test_migration_keeps_the_display_fields_already_on_the_instance(monkeypatch):
    """展示信息本就长在实例行上，补列搬配置时不得把它丢掉。"""
    engine = sa.create_engine("sqlite://")
    with engine.connect() as connection:
        _create_legacy_schema(connection)
        migration = _bind_migration(monkeypatch, connection)

        migration.upgrade()

        assert _instance_rows(connection)["DemoPluginwork"]["plugin_name"] == "分身甲"


def test_migration_does_not_backfill_rows_for_plugins_without_config(monkeypatch):
    """装了却从没存过配置的插件不该凭空多出一行：没有配置就没有要搬的东西。"""
    engine = sa.create_engine("sqlite://")
    with engine.connect() as connection:
        _create_legacy_schema(connection)
        migration = _bind_migration(monkeypatch, connection)

        migration.upgrade()

        assert set(_instance_rows(connection)) == {"DemoPlugin", "DemoPluginwork"}


def test_migration_is_idempotent_on_repeated_upgrade(monkeypatch):
    """重复升级不得因唯一键撞车而失败，也不得把配置复制成两份。"""
    engine = sa.create_engine("sqlite://")
    with engine.connect() as connection:
        _create_legacy_schema(connection)
        migration = _bind_migration(monkeypatch, connection)

        migration.upgrade()
        migration.upgrade()

        rows = _instance_rows(connection)
        assert len(rows) == 2
        assert rows["DemoPlugin"]["config_data"] == {"enable": True, "token": "keep"}


def test_downgrade_puts_plugin_config_back_into_system_settings(monkeypatch):
    """回滚把配置搬回原键并移除新增列，回退路径上不丢配置。"""
    engine = sa.create_engine("sqlite://")
    with engine.connect() as connection:
        _create_legacy_schema(connection)
        migration = _bind_migration(monkeypatch, connection)
        migration.upgrade()

        migration.downgrade()

        assert _system_keys(connection) == {
            "UserInstalledPlugins",
            "plugin.DemoPlugin",
            "plugin.DemoPluginwork",
        }
        assert "config_data" not in _columns(connection)
        # 实例行本身留着：回滚只把配置搬走，不该顺手删掉分身
        assert set(
            row[0]
            for row in connection.execute(
                sa.text("SELECT instance_id FROM plugininstance")
            ).fetchall()
        ) == {"DemoPlugin", "DemoPluginwork"}
