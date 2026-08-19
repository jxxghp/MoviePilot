"""插件配置迁入插件实例配置表的迁移行为：27c3b2eb9b1e_3_0_10。

覆盖存量 ``plugin.<插件ID>`` 行的搬迁正确性、启用态推导（``enable``/``enabled``
两种写法及都没有的情形）、非插件键不受影响、原键必须被删除，以及「存在分身实例
数据时拒绝降级」这条防丢配置的硬约束。
"""

import importlib

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.db.models.pluginconfig import PluginConfig
from app.db.models.systemconfig import SystemConfig
from app.runtime.extensions.instance import DEFAULT_INSTANCE_ID


MIGRATION = "database.versions.27c3b2eb9b1e_3_0_10"


def _bind_migration(monkeypatch, connection):
    """把迁移绑定到隔离数据库连接。"""
    migration = importlib.import_module(MIGRATION)
    context = MigrationContext.configure(connection)
    monkeypatch.setattr(migration, "op", Operations(context))
    return migration


def _build_schema(connection) -> tuple[sa.Table, sa.Table]:
    """建立迁移所需的 systemconfig 与 pluginconfig 表。"""
    metadata = sa.MetaData()
    systemconfig = sa.Table(
        "systemconfig",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("key", sa.String()),
        sa.Column("value", sa.JSON()),
    )
    pluginconfig = sa.Table(
        "pluginconfig",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("plugin_id", sa.String(), nullable=False),
        sa.Column("instance_id", sa.String(), nullable=False, server_default="default"),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("log_level", sa.String()),
        sa.Column("log_expires_at", sa.DateTime()),
        sa.Column("config_data", sa.JSON()),
        sa.Column("plugin_version", sa.String()),
        sa.Column("follow_default_version", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.String()),
        sa.Column("updated_at", sa.String()),
    )
    metadata.create_all(connection)
    return systemconfig, pluginconfig


def test_migration_moves_plugin_rows_and_derives_enabled_state(monkeypatch) -> None:
    """
    存量 plugin.% 行需搬入 pluginconfig 默认实例，启用态按 enable/enabled 推导，
    非插件键保持原样，原插件配置键必须被删除。
    """
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        systemconfig, pluginconfig = _build_schema(connection)
        connection.execute(systemconfig.insert(), [
            {"key": "plugin.PluginEnableKey", "value": {"enable": True, "cron": "5 4 * * *"}},
            {"key": "plugin.PluginEnabledKey", "value": {"enabled": False, "x": 1}},
            {"key": "plugin.PluginNoEnableKey", "value": {"x": 1}},
            {"key": "UserInstalledPlugins", "value": ["PluginEnableKey", "PluginEnabledKey"]},
            {"key": "PluginFolders", "value": {}},
        ])

        migration = _bind_migration(monkeypatch, connection)
        migration.upgrade()
        migration.upgrade()  # 幂等：第二次运行时已无 plugin.% 行，不产生重复数据

        remaining_keys = {
            row[0] for row in connection.execute(sa.select(systemconfig.c.key)).fetchall()
        }
        assert remaining_keys == {"UserInstalledPlugins", "PluginFolders"}

        migrated = {
            row.plugin_id: (row.instance_id, row.is_enabled, row.config_data)
            for row in connection.execute(
                sa.select(
                    pluginconfig.c.plugin_id,
                    pluginconfig.c.instance_id,
                    pluginconfig.c.is_enabled,
                    pluginconfig.c.config_data,
                )
            ).fetchall()
        }
        assert migrated["PluginEnableKey"] == (
            DEFAULT_INSTANCE_ID, True, {"enable": True, "cron": "5 4 * * *"},
        )
        assert migrated["PluginEnabledKey"] == (
            DEFAULT_INSTANCE_ID, False, {"enabled": False, "x": 1},
        )
        assert migrated["PluginNoEnableKey"] == (
            DEFAULT_INSTANCE_ID, False, {"x": 1},
        )


def test_migration_moves_rows_when_tables_created_via_create_all(monkeypatch) -> None:
    """
    表由 ``create_all``（即全新安装 ``init_db()`` 的建表方式）而非本迁移建立时同样必须成功。

    ``PluginConfig`` 的 ``is_enabled``/``follow_default_version`` 只在 ORM 层声明了
    Python 侧默认值，``create_all`` 产出的 DDL 不带数据库端 DEFAULT；若迁移的插入
    语句遗漏这些列，全新安装场景会在这里被 NOT NULL 约束拒绝，而独立建表的测试
    （使用带 server_default 的手写表结构）看不出这个问题。
    """
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        SystemConfig.__table__.create(connection)
        PluginConfig.__table__.create(connection)
        connection.execute(
            sa.insert(SystemConfig.__table__),
            [{"key": "plugin.PluginCreateAll", "value": {"enable": True}}],
        )

        migration = _bind_migration(monkeypatch, connection)
        migration.upgrade()

        row = connection.execute(
            sa.select(PluginConfig.__table__).where(
                PluginConfig.__table__.c.plugin_id == "PluginCreateAll"
            )
        ).mappings().first()
        assert row is not None
        assert row["instance_id"] == DEFAULT_INSTANCE_ID
        assert row["is_enabled"] is True
        assert row["follow_default_version"] is True


def test_migration_downgrade_restores_systemconfig_rows(monkeypatch) -> None:
    """降级需把默认实例配置迁回 systemconfig 对应键，并清空搬迁出的实例配置行。"""
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        systemconfig, pluginconfig = _build_schema(connection)
        connection.execute(systemconfig.insert(), [
            {"key": "plugin.PluginX", "value": {"enable": True}},
        ])

        migration = _bind_migration(monkeypatch, connection)
        migration.upgrade()
        migration.downgrade()

        values = {
            row[0]: row[1]
            for row in connection.execute(
                sa.select(systemconfig.c.key, systemconfig.c.value)
            ).fetchall()
        }
        assert values["plugin.PluginX"] == {"enable": True}

        remaining_pluginconfig = connection.execute(
            sa.select(pluginconfig.c.plugin_id)
        ).fetchall()
        assert remaining_pluginconfig == []


def test_migration_downgrade_rejects_when_non_default_instance_rows_exist(monkeypatch) -> None:
    """存在插件分身（非默认实例）的配置行时拒绝降级，避免默默丢失分身配置。"""
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        systemconfig, pluginconfig = _build_schema(connection)
        connection.execute(pluginconfig.insert(), [
            {
                "plugin_id": "PluginY",
                "instance_id": "clone-a",
                "is_enabled": True,
                "config_data": {"enable": True},
            },
        ])

        migration = _bind_migration(monkeypatch, connection)

        with pytest.raises(RuntimeError, match="非默认实例"):
            migration.downgrade()

        # 拒绝后不得发生部分搬迁
        assert connection.execute(sa.select(systemconfig.c.key)).fetchall() == []
        remaining = connection.execute(sa.select(pluginconfig.c.plugin_id)).fetchall()
        assert remaining == [("PluginY",)]
