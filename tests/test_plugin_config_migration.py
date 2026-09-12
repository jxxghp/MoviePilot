"""插件配置迁入插件实例表的 Alembic 迁移测试。"""

from __future__ import annotations

import importlib
import json

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

MIGRATION_MODULE = "database.versions.c4e1a7b9d2f6_3_0_37"


def _bind_migration(monkeypatch, connection):
    """把迁移绑定到隔离数据库连接。"""
    migration = importlib.import_module(MIGRATION_MODULE)
    context = MigrationContext.configure(connection)
    monkeypatch.setattr(migration, "op", Operations(context))
    return migration


def _create_legacy_schema(connection: sa.engine.Connection) -> None:
    """建出迁移前的系统设置表与实例表，并写入两类数据各若干条。"""
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
        sa.Column("mode", sa.String(16), nullable=False, server_default="virtual"),
        sa.Column("log_level", sa.String(16)),
        sa.Column("log_expires_at", sa.String(40)),
        sa.Column(
            "is_default_target", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.Column("updated_at", sa.String(40), nullable=False),
        sa.CheckConstraint("mode IN ('virtual', 'host')", name="ck_plugininstance_mode"),
    ).create(connection)
    connection.execute(
        sa.text(
            "INSERT INTO plugininstance "
            "(instance_id, source_plugin_id, plugin_name, mode, log_level, "
            " is_default_target, created_at, updated_at) "
            "VALUES (:iid, :sid, :name, :mode, :level, 0, 'x', 'x')"
        ),
        [
            {
                "iid": "DemoPluginwork",
                "sid": "DemoPlugin",
                "name": "分身甲",
                "mode": "virtual",
                "level": "debug",
            },
        ],
    )


def _instance_rows(connection: sa.engine.Connection) -> dict:
    """读取实例表内容，兼容驱动把 JSON 列返回为字符串的情形。"""
    rows = connection.execute(
        sa.text(
            "SELECT instance_id, source_plugin_id, config_data, is_enabled FROM plugininstance"
        )
    ).fetchall()
    return {
        row[0]: {
            "source_plugin_id": row[1],
            "config_data": json.loads(row[2]) if isinstance(row[2], str) else row[2],
            "is_enabled": bool(row[3]),
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


def test_migration_keeps_existing_instances_enabled(monkeypatch):
    """存量实例此刻都被装载着，补出启用位不得把它们整批判成未启用。"""
    engine = sa.create_engine("sqlite://")
    with engine.connect() as connection:
        _create_legacy_schema(connection)
        migration = _bind_migration(monkeypatch, connection)

        migration.upgrade()

        assert all(row["is_enabled"] for row in _instance_rows(connection).values())


def test_migration_drops_the_redundant_mode_column(monkeypatch):
    """mode 是从身份等式复制出来的冗余副本，迁移后不再存在。"""
    engine = sa.create_engine("sqlite://")
    with engine.connect() as connection:
        _create_legacy_schema(connection)
        migration = _bind_migration(monkeypatch, connection)

        migration.upgrade()

        columns = _columns(connection)
        assert "mode" not in columns
        assert {"is_enabled", "config_data"} <= columns


def test_migration_preserves_the_log_level_already_on_the_instance(monkeypatch):
    """日志等级覆盖本就长在实例行上，删列重建表时不得把它丢掉。"""
    engine = sa.create_engine("sqlite://")
    with engine.connect() as connection:
        _create_legacy_schema(connection)
        migration = _bind_migration(monkeypatch, connection)

        migration.upgrade()

        level = connection.execute(
            sa.text("SELECT log_level FROM plugininstance WHERE instance_id = 'DemoPluginwork'")
        ).scalar_one()
        assert level == "debug"


def test_migration_is_idempotent_on_repeated_upgrade(monkeypatch):
    """重复升级不得因唯一键撞车而失败，也不得把配置复制成两份。"""
    engine = sa.create_engine("sqlite://")
    with engine.connect() as connection:
        _create_legacy_schema(connection)
        migration = _bind_migration(monkeypatch, connection)

        migration.upgrade()
        migration.upgrade()

        # 两个来自配置迁移、一个来自安装清单回填，重复升级不得再翻一倍
        assert len(_instance_rows(connection)) == 3


def test_downgrade_puts_plugin_config_back_into_system_settings(monkeypatch):
    """回滚把配置搬回原键并还原 mode 列，回退路径上不丢配置。"""
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
        columns = _columns(connection)
        assert "mode" in columns
        assert "config_data" not in columns
        modes = dict(
            connection.execute(sa.text("SELECT instance_id, mode FROM plugininstance")).fetchall()
        )
        assert modes == {
            "DemoPlugin": "host",
            "DemoPluginwork": "virtual",
            "BareInstalled": "host",
        }


def test_migration_backfills_host_rows_for_installed_plugins(monkeypatch):
    """装了却从没存过配置的插件也要拿到一行启用中的本体记录。

    本体的装载判据归口到 is_enabled 之后，安装清单不再兼任运行开关；不回填这些行，
    升级后「装了但没配置过」的插件会整批不再加载。
    """
    engine = sa.create_engine("sqlite://")
    with engine.connect() as connection:
        _create_legacy_schema(connection)
        migration = _bind_migration(monkeypatch, connection)

        migration.upgrade()

        rows = _instance_rows(connection)
        assert "BareInstalled" in rows
        assert rows["BareInstalled"]["is_enabled"] is True
        assert rows["BareInstalled"]["source_plugin_id"] == "BareInstalled"
        # 没有配置就不该凭空造出配置
        assert rows["BareInstalled"]["config_data"] is None
