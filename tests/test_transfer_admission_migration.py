"""整理任务持久准入字段的 Alembic 迁移测试。"""

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

MIGRATION = "database.versions.b1e7d3f5a9c2_3_0_13"
ADMISSION_COLUMNS = {
    "id",
    "task_id",
    "storage",
    "src_path",
    "created_at",
    "state",
    "updated_at",
    "last_error",
}


def _bind_migration(monkeypatch, connection):
    """把迁移绑定到隔离数据库连接。"""
    migration = importlib.import_module(MIGRATION)
    monkeypatch.setattr(
        migration,
        "op",
        Operations(MigrationContext.configure(connection)),
    )
    return migration


def _create_legacy_table(connection) -> None:
    """创建 3.0.12 时代的待整理登记表。"""
    metadata = sa.MetaData()
    table = sa.Table(
        "transferpending",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("storage", sa.String(), nullable=False),
        sa.Column("src_path", sa.String(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=True),
    )
    sa.Index(
        "ux_transferpending_storage_path",
        table.c.storage,
        table.c.src_path,
        unique=True,
    )
    metadata.create_all(connection)
    connection.execute(table.insert(), [
        {
            "id": 1,
            "storage": "local",
            "src_path": "/mnt/dated.mkv",
            "created_at": "2026-08-26 10:00:00",
        },
        {
            "id": 2,
            "storage": "alist",
            "src_path": "/mnt/undated.mkv",
            "created_at": None,
        },
    ])


def _rows(connection) -> list[dict[str, object]]:
    """读取迁移后的准入字段快照。"""
    pending = sa.table(
        "transferpending",
        sa.column("id", sa.Integer()),
        sa.column("task_id", sa.String()),
        sa.column("state", sa.String()),
        sa.column("created_at", sa.String()),
        sa.column("updated_at", sa.String()),
        sa.column("last_error", sa.Text()),
    )
    return [
        dict(row)
        for row in connection.execute(
            sa.select(pending).order_by(pending.c.id)
        ).mappings().all()
    ]


def test_transfer_admission_upgrade_downgrade_reupgrade(
    monkeypatch,
) -> None:
    """旧行应保守回填，且 SQLite 支持重复升级、降级和再次升级。"""
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        _create_legacy_table(connection)
        migration = _bind_migration(monkeypatch, connection)

        migration.upgrade()
        migration.upgrade()

        inspector = sa.inspect(connection)
        assert {
            column["name"]
            for column in inspector.get_columns("transferpending")
        } == ADMISSION_COLUMNS
        constraints = {
            constraint["name"]
            for constraint in inspector.get_unique_constraints("transferpending")
        }
        assert "uq_transferpending_task_id" in constraints
        assert {
            index["name"]
            for index in inspector.get_indexes("transferpending")
        } == {
            "ix_transferpending_state_created",
            "ux_transferpending_storage_path",
        }

        upgraded = _rows(connection)
        first_task_ids = [row["task_id"] for row in upgraded]
        assert all(first_task_ids)
        assert len(set(first_task_ids)) == 2
        assert {row["state"] for row in upgraded} == {"accepted"}
        assert upgraded[0]["updated_at"] == upgraded[0]["created_at"]
        assert upgraded[1]["updated_at"]
        assert {row["last_error"] for row in upgraded} == {None}

        migration.downgrade()
        downgraded_inspector = sa.inspect(connection)
        assert {
            column["name"]
            for column in downgraded_inspector.get_columns("transferpending")
        } == {"id", "storage", "src_path", "created_at"}
        assert {
            index["name"]
            for index in downgraded_inspector.get_indexes("transferpending")
        } == {"ux_transferpending_storage_path"}
        legacy_rows = connection.execute(
            sa.text(
                "SELECT id, storage, src_path, created_at "
                "FROM transferpending ORDER BY id"
            )
        ).mappings().all()
        assert [row["src_path"] for row in legacy_rows] == [
            "/mnt/dated.mkv",
            "/mnt/undated.mkv",
        ]

        migration.upgrade()
        reupgraded = _rows(connection)
        assert [row["task_id"] for row in reupgraded] == first_task_ids
        assert {row["state"] for row in reupgraded} == {"accepted"}
        assert {
            index["name"]
            for index in sa.inspect(connection).get_indexes("transferpending")
        } == {
            "ix_transferpending_state_created",
            "ux_transferpending_storage_path",
        }


def test_transfer_admission_migration_runs_on_postgresql(monkeypatch) -> None:
    """隔离 PostgreSQL 应真实执行准入字段、约束、索引和可逆回滚。"""
    prefix = "MOVIEPILOT_TEST_POSTGRESQL_"
    host = os.getenv(f"{prefix}HOST")
    database = os.getenv(f"{prefix}DATABASE")
    username = os.getenv(f"{prefix}USERNAME")
    if not host or not database or not username:
        pytest.skip("未配置隔离 PostgreSQL migration 测试库")

    port = os.getenv(f"{prefix}PORT", "5432")
    password = os.getenv(f"{prefix}PASSWORD", "")
    schema = f"transfer_admission_{uuid.uuid4().hex}"
    with postgres_driver.connect(
        host=host,
        port=port,
        dbname=database,
        user=username,
        password=password,
    ) as connection:
        connection.autocommit = True
        with connection.cursor() as cursor:
            cursor.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

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
            _create_legacy_table(connection)
            migration = _bind_migration(monkeypatch, connection)
            migration.upgrade()
            migration.upgrade()

            inspector = sa.inspect(connection)
            assert {
                constraint["name"]
                for constraint in inspector.get_unique_constraints(
                    "transferpending"
                )
            } >= {"uq_transferpending_task_id"}
            assert {
                index["name"]
                for index in inspector.get_indexes("transferpending")
            } >= {"ix_transferpending_state_created"}
            assert all(row["task_id"] for row in _rows(connection))

            migration.downgrade()
            assert {
                column["name"]
                for column in sa.inspect(connection).get_columns("transferpending")
            } == {"id", "storage", "src_path", "created_at"}
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
