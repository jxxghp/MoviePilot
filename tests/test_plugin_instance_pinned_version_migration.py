"""插件实例版本绑定合并为单列的 Alembic 迁移测试。"""

from __future__ import annotations

import importlib
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

MIGRATION_MODULE = "database.versions.f3b8c2d5e7a1_3_0_38"


def _bind_migration(monkeypatch, connection):
    """把迁移绑定到隔离数据库连接。"""
    migration = importlib.import_module(MIGRATION_MODULE)
    context = MigrationContext.configure(connection)
    monkeypatch.setattr(migration, "op", Operations(context))
    return migration


def _create_legacy_table(connection: sa.engine.Connection) -> None:
    """建出合并前的双列结构，并写入跟随与锚定两种实例。"""
    now = datetime.now(timezone.utc).isoformat()
    table = sa.Table(
        "plugininstance",
        sa.MetaData(),
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("instance_id", sa.String(length=128), nullable=False),
        sa.Column("source_plugin_id", sa.String(length=128), nullable=False),
        sa.Column("plugin_version", sa.String(length=64)),
        sa.Column("follow_current_version", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.String(length=40), nullable=False),
        sa.Column("updated_at", sa.String(length=40), nullable=False),
    )
    table.create(connection)
    connection.execute(
        table.insert(),
        [
            {
                "instance_id": "DemoPluginPinned",
                "source_plugin_id": "DemoPlugin",
                "plugin_version": "1.0.0",
                "follow_current_version": False,
                "created_at": now,
                "updated_at": now,
            },
            {
                # 跟随当前版本却留着旧 plugin_version——正是被合并消灭的非法组合
                "instance_id": "DemoPluginFollowing",
                "source_plugin_id": "DemoPlugin",
                "plugin_version": "0.9.0",
                "follow_current_version": True,
                "created_at": now,
                "updated_at": now,
            },
        ],
    )


def _rows(connection: sa.engine.Connection, column: str) -> dict:
    """按实例 ID 读取指定列。"""
    return {
        row[0]: row[1]
        for row in connection.execute(
            sa.text(f"SELECT instance_id, {column} FROM plugininstance")
        ).fetchall()
    }


def test_migration_keeps_only_the_pinned_version(monkeypatch):
    """锚定实例保留版本号，跟随实例的陈旧版本号一并丢弃。"""
    engine = sa.create_engine("sqlite://")
    with engine.connect() as connection:
        _create_legacy_table(connection)
        migration = _bind_migration(monkeypatch, connection)

        migration.upgrade()

        assert _rows(connection, "pinned_version") == {
            "DemoPluginPinned": "1.0.0",
            # 跟随当前版本即为空：留着旧值正是「绑定落空」那一类缺陷的来源
            "DemoPluginFollowing": None,
        }
        columns = {c["name"] for c in sa.inspect(connection).get_columns("plugininstance")}
        assert "plugin_version" not in columns
        assert "follow_current_version" not in columns


def test_migration_is_idempotent_on_repeated_upgrade(monkeypatch):
    """重复升级不得因列已删除而失败。"""
    engine = sa.create_engine("sqlite://")
    with engine.connect() as connection:
        _create_legacy_table(connection)
        migration = _bind_migration(monkeypatch, connection)

        migration.upgrade()
        migration.upgrade()

        assert _rows(connection, "pinned_version")["DemoPluginPinned"] == "1.0.0"


def test_downgrade_restores_both_columns(monkeypatch):
    """回滚还原双列，并按是否锚定回填跟随开关。"""
    engine = sa.create_engine("sqlite://")
    with engine.connect() as connection:
        _create_legacy_table(connection)
        migration = _bind_migration(monkeypatch, connection)
        migration.upgrade()

        migration.downgrade()

        assert _rows(connection, "plugin_version") == {
            "DemoPluginPinned": "1.0.0",
            "DemoPluginFollowing": None,
        }
        assert _rows(connection, "follow_current_version") == {
            "DemoPluginPinned": 0,
            "DemoPluginFollowing": 1,
        }
