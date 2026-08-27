"""整理规划输入与检查点字段的 Alembic 迁移测试。"""

import importlib
import json
import os
import uuid

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.application.transfer import TransferPlanningInput
from app.db.models.transferpending import TransferPending

try:
    import psycopg2 as postgres_driver
    from psycopg2 import sql

    POSTGRESQL_DIALECT = "postgresql+psycopg2"
except ModuleNotFoundError:
    import psycopg as postgres_driver
    from psycopg import sql

    POSTGRESQL_DIALECT = "postgresql+psycopg"

MIGRATION = "database.versions.c2f8a4d6e1b3_3_0_14"


def _bind_migration(monkeypatch, connection):
    """把迁移绑定到隔离数据库连接。"""
    migration = importlib.import_module(MIGRATION)
    monkeypatch.setattr(
        migration,
        "op",
        Operations(MigrationContext.configure(connection)),
    )
    return migration


def _create_admission_table(connection) -> None:
    """创建 3.0.13 时代的持久准入表。"""
    metadata = sa.MetaData()
    table = sa.Table(
        "transferpending",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("task_id", sa.String(64), nullable=False),
        sa.Column("storage", sa.String(), nullable=False),
        sa.Column("src_path", sa.String(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=True),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("updated_at", sa.String(40), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.UniqueConstraint("task_id", name="uq_transferpending_task_id"),
    )
    sa.Index(
        "ux_transferpending_storage_path",
        table.c.storage,
        table.c.src_path,
        unique=True,
    )
    sa.Index(
        "ix_transferpending_state_created",
        table.c.state,
        table.c.created_at,
        table.c.id,
    )
    metadata.create_all(connection)
    connection.execute(table.insert(), {
        "id": 1,
        "task_id": "stable-task",
        "storage": "local",
        "src_path": "/downloads/Movie.mkv",
        "created_at": "2026-08-27 10:00:00",
        "state": "accepted",
        "updated_at": "2026-08-27 10:00:00",
        "last_error": "previous enqueue failure",
    })


def _planning_row(connection) -> dict[str, object]:
    """读取单条规划持久字段快照。"""
    return dict(connection.execute(sa.text(
        "SELECT task_id, state, input_version, planning_input, "
        "input_fingerprint, checkpoint_version, checkpoint_payload, planned_at "
        "FROM transferpending WHERE id = 1"
    )).mappings().one())


def _assert_upgrade_downgrade_reupgrade(connection, monkeypatch) -> None:
    """断言规划迁移在当前隔离连接上的完整可逆生命周期。"""
    _create_admission_table(connection)
    migration = _bind_migration(monkeypatch, connection)

    migration.upgrade()
    migration.upgrade()

    inspector = sa.inspect(connection)
    assert {
        column["name"]
        for column in inspector.get_columns("transferpending")
    } == {column.name for column in TransferPending.__table__.columns}
    upgraded = _planning_row(connection)
    planning_payload = upgraded["planning_input"]
    if isinstance(planning_payload, str):
        planning_payload = json.loads(planning_payload)
    planning_input = TransferPlanningInput.from_payload(planning_payload)
    assert planning_input == TransferPlanningInput.legacy(
        storage="local",
        src_path="/downloads/Movie.mkv",
    )
    assert upgraded["input_version"] == 1
    assert upgraded["input_fingerprint"] == planning_input.fingerprint
    assert upgraded["checkpoint_payload"] is None
    assert upgraded["state"] == "accepted"

    pending = sa.table(
        "transferpending",
        sa.column("id", sa.Integer()),
        sa.column("state", sa.String()),
        sa.column("checkpoint_version", sa.Integer()),
        sa.column("checkpoint_payload", sa.JSON()),
        sa.column("planned_at", sa.String()),
    )
    connection.execute(
        pending.update()
        .where(pending.c.id == 1)
        .values(
            state="planned",
            checkpoint_version=1,
            checkpoint_payload={"schema_version": 1},
            planned_at="2026-08-27 11:00:00",
        )
    )
    migration.downgrade()

    downgraded = sa.inspect(connection)
    assert {
        column["name"]
        for column in downgraded.get_columns("transferpending")
    } == {
        "id", "task_id", "storage", "src_path", "created_at",
        "state", "updated_at", "last_error",
    }
    legacy = connection.execute(sa.text(
        "SELECT task_id, state FROM transferpending WHERE id = 1"
    )).mappings().one()
    assert dict(legacy) == {"task_id": "stable-task", "state": "accepted"}
    assert {
        index["name"]
        for index in downgraded.get_indexes("transferpending")
    } == {"ix_transferpending_state_created", "ux_transferpending_storage_path"}

    migration.upgrade()
    reupgraded = _planning_row(connection)
    assert reupgraded["task_id"] == "stable-task"
    assert reupgraded["state"] == "accepted"
    assert reupgraded["checkpoint_payload"] is None
    assert reupgraded["input_fingerprint"] == planning_input.fingerprint


def test_transfer_planning_upgrade_downgrade_reupgrade(monkeypatch) -> None:
    """SQLite 应支持规划字段重复升级、降级和再次升级。"""
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        _assert_upgrade_downgrade_reupgrade(connection, monkeypatch)


def test_provider_pending_downgrade_restores_accepted_on_sqlite(monkeypatch) -> None:
    """旧版本无法解释 provider 快照，降级时必须保守恢复为 accepted。"""
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        _create_admission_table(connection)
        migration = _bind_migration(monkeypatch, connection)
        migration.upgrade()
        connection.execute(sa.text(
            "UPDATE transferpending "
            "SET state = 'provider_pending', checkpoint_version = 1, "
            "checkpoint_payload = '{\"schema_version\": 1}' "
            "WHERE id = 1"
        ))

        migration.downgrade()

        assert connection.execute(sa.text(
            "SELECT state FROM transferpending WHERE id = 1"
        )).scalar_one() == "accepted"
        assert {
            column["name"]
            for column in sa.inspect(connection).get_columns("transferpending")
        } == {
            "id", "task_id", "storage", "src_path", "created_at",
            "state", "updated_at", "last_error",
        }


def test_partial_upgrade_preserves_existing_planning_json(monkeypatch) -> None:
    """迁移中断后重跑应补齐版本和指纹，不得覆盖已经写入的完整输入。"""
    engine = sa.create_engine("sqlite://")
    planning_input = TransferPlanningInput(
        source_fileitem={
            "storage": "local",
            "path": "/downloads/Movie.mkv",
            "type": "file",
        },
        target_storage="local",
        target_path="/library/Movies",
        requested_transfer_type="copy",
        options={"manual": True},
    )
    with engine.begin() as connection:
        _create_admission_table(connection)
        connection.execute(sa.text(
            "ALTER TABLE transferpending ADD COLUMN planning_input JSON"
        ))
        pending = sa.table(
            "transferpending",
            sa.column("id", sa.Integer()),
            sa.column("planning_input", sa.JSON()),
        )
        connection.execute(
            pending.update()
            .where(pending.c.id == 1)
            .values(planning_input=planning_input.to_payload())
        )

        migration = _bind_migration(monkeypatch, connection)
        migration.upgrade()
        upgraded = _planning_row(connection)
        payload = upgraded["planning_input"]
        if isinstance(payload, str):
            payload = json.loads(payload)

        assert TransferPlanningInput.from_payload(payload) == planning_input
        assert upgraded["input_version"] == planning_input.schema_version
        assert upgraded["input_fingerprint"] == planning_input.fingerprint

        connection.execute(sa.text(
            "UPDATE transferpending SET state = 'future-state' WHERE id = 1"
        ))
        migration.downgrade()
        assert connection.execute(sa.text(
            "SELECT state FROM transferpending WHERE id = 1"
        )).scalar_one() == "future-state"


def test_transfer_planning_migration_runs_on_postgresql(monkeypatch) -> None:
    """配置隔离 PostgreSQL 时真实验证规划字段的完整可逆迁移。"""
    prefix = "MOVIEPILOT_TEST_POSTGRESQL_"
    host = os.getenv(f"{prefix}HOST")
    database = os.getenv(f"{prefix}DATABASE")
    username = os.getenv(f"{prefix}USERNAME")
    if not host or not database or not username:
        pytest.skip("未配置隔离 PostgreSQL migration 测试库")

    port = os.getenv(f"{prefix}PORT", "5432")
    password = os.getenv(f"{prefix}PASSWORD", "")
    schema = f"transfer_planning_{uuid.uuid4().hex}"
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
            _assert_upgrade_downgrade_reupgrade(connection, monkeypatch)
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
