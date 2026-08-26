"""插件身份表 Alembic 迁移测试。"""

import importlib
import os
import uuid

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

try:
    import psycopg2 as postgres_driver
    from psycopg2 import sql
    POSTGRESQL_DIALECT = "postgresql+psycopg2"
except ModuleNotFoundError:
    import psycopg as postgres_driver
    from psycopg import sql
    POSTGRESQL_DIALECT = "postgresql+psycopg"

from app.db.models.pluginidentity import PluginIdentity

BASE_MIGRATION = "database.versions.d2e4f6a8b0c1_3_0_9"
INSTALLATION_MIGRATION = "database.versions.e4f7a1b2c3d5_3_0_10"
DECLARATION_MIGRATION = "database.versions.5f2a9c1e7b4d_3_0_12"


def _bind_migration(
    monkeypatch,
    connection,
    module_name: str = BASE_MIGRATION,
):
    """把迁移绑定到隔离数据库连接。"""
    migration = importlib.import_module(module_name)
    context = MigrationContext.configure(connection)
    monkeypatch.setattr(migration, "op", Operations(context))
    return migration


def test_plugin_identity_migration_upgrades_twice_and_downgrades(
    monkeypatch,
) -> None:
    """SQLite 应可从旧身份表升级到声明快照并按逆序完整回滚。"""
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        migration = _bind_migration(monkeypatch, connection)

        migration.upgrade()
        migration.upgrade()
        installation = _bind_migration(
            monkeypatch,
            connection,
            INSTALLATION_MIGRATION,
        )
        installation.upgrade()
        installation.upgrade()
        declaration = _bind_migration(
            monkeypatch,
            connection,
            DECLARATION_MIGRATION,
        )
        declaration.upgrade()
        declaration.upgrade()

        inspector = sa.inspect(connection)
        assert "pluginidentity" in inspector.get_table_names()
        columns = {
            column["name"] for column in inspector.get_columns("pluginidentity")
        }
        assert columns == {column.name for column in PluginIdentity.__table__.columns}
        unique_constraints = {
            constraint["name"]: tuple(constraint["column_names"])
            for constraint in inspector.get_unique_constraints("pluginidentity")
        }
        assert unique_constraints["uq_pluginidentity_normalized_plugin_id"] == (
            "normalized_plugin_id",
        )
        check_constraints = {
            constraint["name"]
            for constraint in inspector.get_check_constraints("pluginidentity")
        }
        assert check_constraints == {
            "ck_pluginidentity_normalized_plugin_id",
            "ck_pluginidentity_revision",
        }
        assert "plugininstallation" in inspector.get_table_names()

        declaration.downgrade()
        installation.downgrade()
        migration.downgrade()
        assert not {
            "pluginidentity",
            "plugininstallation",
        } & set(sa.inspect(connection).get_table_names())

        migration.upgrade()
        installation.upgrade()
        declaration.upgrade()
        assert {
            column["name"]
            for column in sa.inspect(connection).get_columns("pluginidentity")
        } == {column.name for column in PluginIdentity.__table__.columns}


def test_plugin_identity_migration_accepts_fresh_current_schema(monkeypatch) -> None:
    """create_all 已建当前表时重复升级不得创建冲突对象。"""
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        PluginIdentity.__table__.create(connection)
        migration = _bind_migration(
            monkeypatch,
            connection,
            DECLARATION_MIGRATION,
        )

        migration.upgrade()
        migration.upgrade()

        assert {
            column["name"]
            for column in sa.inspect(connection).get_columns("pluginidentity")
        } == {column.name for column in PluginIdentity.__table__.columns}


def test_plugin_identity_declaration_migration_backfills_and_restores_legacy_fields(
    monkeypatch,
) -> None:
    """声明迁移应保守回填旧字段，并能在降级时恢复旧投影。"""
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        base = _bind_migration(monkeypatch, connection)
        base.upgrade()
        table = sa.Table(
            "pluginidentity",
            sa.MetaData(),
            autoload_with=connection,
        )
        connection.execute(
            table.insert().values(
                plugin_id="DemoPlugin",
                normalized_plugin_id="demoplugin",
                trusted_source_type="official",
                trusted_source_key="github:jxxghp/moviepilot-plugins",
                binding_basis="official_default",
                payload_source_type="official",
                payload_source_key="github:jxxghp/moviepilot-plugins",
                declared_version="1.0.0",
                package_generation="v3",
                system_version=">=3.0.0",
                supports_v3=True,
                supports_v3t=False,
                payload_receipt="sha256:" + "0" * 64,
                revision=1,
                created_at="2026-08-25T12:00:00+00:00",
                updated_at="2026-08-25T12:00:00+00:00",
                bound_at="2026-08-25T12:00:00+00:00",
                payload_applied_at="2026-08-25T12:00:00+00:00",
            )
        )
        unknown_id = connection.execute(
            table.insert().values(
                plugin_id="UnknownPlugin",
                normalized_plugin_id="unknownplugin",
                trusted_source_type="unknown",
                binding_basis="legacy_unbound",
                payload_source_type="unknown",
                revision=1,
                created_at="2026-08-25T12:00:00+00:00",
                updated_at="2026-08-25T12:00:00+00:00",
            ).returning(table.c.id)
        ).scalar_one()

        declaration = _bind_migration(
            monkeypatch,
            connection,
            DECLARATION_MIGRATION,
        )
        declaration.upgrade()
        current = sa.Table(
            "pluginidentity",
            sa.MetaData(),
            autoload_with=connection,
        )
        row = connection.execute(
            sa.select(current).where(current.c.plugin_id == "DemoPlugin")
        ).mappings().one()
        assert row["declared_metadata"] == {
            "schema_version": 1,
            "declaration_version": None,
            "manifest_matches_payload": False,
            "manifest": {"system_version": ">=3.0.0"},
            "runtime": {"v3": True, "v3t": False},
        }
        assert connection.execute(
            sa.select(current.c.id).where(current.c.declared_metadata.is_(None))
        ).scalars().all() == [unknown_id]

        declaration.downgrade()
        restored = sa.Table(
            "pluginidentity",
            sa.MetaData(),
            autoload_with=connection,
        )
        restored_row = connection.execute(
            sa.select(restored).where(restored.c.plugin_id == "DemoPlugin")
        ).mappings().one()
        assert restored_row["system_version"] == ">=3.0.0"
        assert restored_row["supports_v3"] is True
        assert restored_row["supports_v3t"] is False
        assert "declared_metadata" not in restored.c


def test_plugin_identity_declaration_migration_rejects_partial_legacy_schema(
    monkeypatch,
) -> None:
    """非 canonical 旧字段集合必须停止迁移，不能静默丢弃残余声明。"""
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        base = _bind_migration(monkeypatch, connection)
        base.upgrade()
        with Operations(MigrationContext.configure(connection)).batch_alter_table(
            "pluginidentity"
        ) as batch_op:
            batch_op.drop_column("supports_v3t")

        declaration = _bind_migration(
            monkeypatch,
            connection,
            DECLARATION_MIGRATION,
        )
        with pytest.raises(RuntimeError, match="旧声明字段不完整"):
            declaration.upgrade()

        columns = {
            column["name"]
            for column in sa.inspect(connection).get_columns("pluginidentity")
        }
        assert "declared_metadata" not in columns
        assert {"system_version", "supports_v3"} <= columns


def test_plugin_identity_migration_matches_postgresql_identity() -> None:
    """独立 Alembic 路径应保留 PostgreSQL 循环 Identity 主键。"""
    migration = importlib.import_module(BASE_MIGRATION)
    metadata = sa.MetaData()
    table = sa.Table(
        "pluginidentity",
        metadata,
        migration._id_column("postgresql"),
        sa.PrimaryKeyConstraint("id"),
    )

    identity = table.c.id.identity
    assert identity is not None
    assert identity.start == 1
    assert identity.cycle is True
    ddl = str(CreateTable(table).compile(dialect=postgresql.dialect()))
    assert "GENERATED BY DEFAULT AS IDENTITY" in ddl
    assert "CYCLE" in ddl


def test_plugin_identity_migration_runs_on_postgresql(monkeypatch) -> None:
    """已配置的隔离 PostgreSQL 应执行真实建表、约束和回滚。"""
    prefix = "MOVIEPILOT_TEST_POSTGRESQL_"
    host = os.getenv(f"{prefix}HOST")
    database = os.getenv(f"{prefix}DATABASE")
    username = os.getenv(f"{prefix}USERNAME")
    if not host or not database or not username:
        pytest.skip("未配置隔离 PostgreSQL migration 测试库")

    port = os.getenv(f"{prefix}PORT", "5432")
    password = os.getenv(f"{prefix}PASSWORD", "")
    schema = f"plugin_identity_{uuid.uuid4().hex}"
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
            installation = _bind_migration(
                monkeypatch,
                connection,
                INSTALLATION_MIGRATION,
            )
            installation.upgrade()
            legacy_table = sa.Table(
                "pluginidentity",
                sa.MetaData(),
                autoload_with=connection,
            )
            inserted_id = connection.execute(
                legacy_table.insert().values(
                    plugin_id="DemoPlugin",
                    normalized_plugin_id="demoplugin",
                    trusted_source_type="official",
                    trusted_source_key="github:jxxghp/moviepilot-plugins",
                    binding_basis="official_default",
                    payload_source_type="official",
                    payload_source_key="github:jxxghp/moviepilot-plugins",
                    declared_version="1.0.0",
                    package_generation="v3",
                    system_version=">=3.0.0",
                    supports_v3=True,
                    supports_v3t=False,
                    payload_receipt="sha256:" + "0" * 64,
                    revision=1,
                    created_at="2026-08-25T12:00:00+00:00",
                    updated_at="2026-08-25T12:00:00+00:00",
                    bound_at="2026-08-25T12:00:00+00:00",
                    payload_applied_at="2026-08-25T12:00:00+00:00",
                ).returning(legacy_table.c.id)
            ).scalar_one()
            assert inserted_id == 1
            unknown_id = connection.execute(
                legacy_table.insert().values(
                    plugin_id="UnknownPlugin",
                    normalized_plugin_id="unknownplugin",
                    trusted_source_type="unknown",
                    binding_basis="legacy_unbound",
                    payload_source_type="unknown",
                    revision=1,
                    created_at="2026-08-25T12:00:00+00:00",
                    updated_at="2026-08-25T12:00:00+00:00",
                ).returning(legacy_table.c.id)
            ).scalar_one()
            assert unknown_id == 2
            declaration = _bind_migration(
                monkeypatch,
                connection,
                DECLARATION_MIGRATION,
            )
            declaration.upgrade()
            declaration.upgrade()

            table = sa.Table(
                "pluginidentity",
                sa.MetaData(),
                autoload_with=connection,
            )
            row = connection.execute(
                sa.select(table).where(table.c.id == inserted_id)
            ).mappings().one()
            assert row["declared_metadata"] == {
                "schema_version": 1,
                "declaration_version": None,
                "manifest_matches_payload": False,
                "manifest": {"system_version": ">=3.0.0"},
                "runtime": {"v3": True, "v3t": False},
            }
            unknown_row = connection.execute(
                sa.select(table).where(table.c.id == unknown_id)
            ).mappings().one()
            assert unknown_row["declared_metadata"] is None

            with pytest.raises(sa.exc.IntegrityError):
                with connection.begin_nested():
                    connection.execute(
                        table.insert().values(
                            plugin_id="UppercaseKey",
                            normalized_plugin_id="UppercaseKey",
                            trusted_source_type="unknown",
                            binding_basis="legacy_unbound",
                            payload_source_type="unknown",
                            revision=1,
                            created_at="2026-08-25T12:00:00+00:00",
                            updated_at="2026-08-25T12:00:00+00:00",
                        )
                    )

            declaration.downgrade()
            restored = sa.Table(
                "pluginidentity",
                sa.MetaData(),
                autoload_with=connection,
            )
            restored_row = connection.execute(
                sa.select(restored).where(restored.c.id == inserted_id)
            ).mappings().one()
            assert restored_row["system_version"] == ">=3.0.0"
            assert restored_row["supports_v3"] is True
            assert restored_row["supports_v3t"] is False

            declaration.upgrade()
            reupgraded = sa.Table(
                "pluginidentity",
                sa.MetaData(),
                autoload_with=connection,
            )
            reupgraded_row = connection.execute(
                sa.select(reupgraded).where(reupgraded.c.id == inserted_id)
            ).mappings().one()
            assert reupgraded_row["declared_metadata"]["runtime"] == {
                "v3": True,
                "v3t": False,
            }

            declaration.downgrade()
            installation.downgrade()
            migration.downgrade()
            assert "pluginidentity" not in sa.inspect(connection).get_table_names()
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
