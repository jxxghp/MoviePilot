"""插件安装事务表 Alembic 迁移测试。"""

import importlib
import os
import uuid

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

try:
    import psycopg2 as postgres_driver
    from psycopg2 import sql

    POSTGRESQL_DIALECT = "postgresql+psycopg2"
except ModuleNotFoundError:
    import psycopg as postgres_driver
    from psycopg import sql

    POSTGRESQL_DIALECT = "postgresql+psycopg"

from app.db.models.plugininstallation import PluginInstallation

MIGRATION = "database.versions.e4f7a1b2c3d5_3_0_10"


def _bind_migration(monkeypatch, connection):
    """把迁移绑定到隔离数据库连接。"""
    migration = importlib.import_module(MIGRATION)
    monkeypatch.setattr(
        migration,
        "op",
        Operations(MigrationContext.configure(connection)),
    )
    return migration


def test_plugin_installation_migration_upgrade_downgrade_reupgrade(
    monkeypatch,
) -> None:
    """SQLite 应支持重复升级、回滚和再次升级，字段与 ORM 保持一致。"""
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        migration = _bind_migration(monkeypatch, connection)

        migration.upgrade()
        migration.upgrade()

        inspector = sa.inspect(connection)
        assert "plugininstallation" in inspector.get_table_names()
        assert {
            column["name"] for column in inspector.get_columns("plugininstallation")
        } == {column.name for column in PluginInstallation.__table__.columns}
        assert {
            index["name"] for index in inspector.get_indexes("plugininstallation")
        } == {
            "ix_plugininstallation_plugin_id",
            "ix_plugininstallation_phase",
        }

        migration.downgrade()
        assert "plugininstallation" not in sa.inspect(connection).get_table_names()

        migration.upgrade()
        assert "plugininstallation" in sa.inspect(connection).get_table_names()


def test_plugin_installation_migration_runs_on_postgresql(monkeypatch) -> None:
    """隔离 PostgreSQL 应真实执行安装事务表的升级、约束和回滚。"""
    prefix = "MOVIEPILOT_TEST_POSTGRESQL_"
    host = os.getenv(f"{prefix}HOST")
    database = os.getenv(f"{prefix}DATABASE")
    username = os.getenv(f"{prefix}USERNAME")
    if not host or not database or not username:
        pytest.skip("未配置隔离 PostgreSQL migration 测试库")

    port = os.getenv(f"{prefix}PORT", "5432")
    password = os.getenv(f"{prefix}PASSWORD", "")
    schema = f"plugin_installation_{uuid.uuid4().hex}"
    with postgres_driver.connect(
        host=host,
        port=port,
        dbname=database,
        user=username,
        password=password,
    ) as connection:
        connection.autocommit = True
        with connection.cursor() as cursor:
            cursor.execute(
                sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema))
            )

    engine = None
    try:
        engine = sa.create_engine(
            sa.URL.create(
                POSTGRESQL_DIALECT,
                username=username,
                password=password,
                host=host,
                port=int(port),
                database=database,
            ),
            connect_args={"options": f"-csearch_path={schema}"},
        )
        with engine.begin() as connection:
            migration = _bind_migration(monkeypatch, connection)
            migration.upgrade()
            migration.upgrade()

            inspector = sa.inspect(connection)
            assert "plugininstallation" in inspector.get_table_names()
            constraints = {
                constraint["name"]
                for constraint in inspector.get_unique_constraints(
                    "plugininstallation"
                )
            }
            assert "uq_plugininstallation_transaction_id" in constraints

            migration.downgrade()
            assert "plugininstallation" not in sa.inspect(connection).get_table_names()
    finally:
        if engine is not None:
            engine.dispose()
        with postgres_driver.connect(
            host=host,
            port=port,
            dbname=database,
            user=username,
            password=password,
        ) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                cursor.execute(
                    sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                        sql.Identifier(schema)
                    )
                )
