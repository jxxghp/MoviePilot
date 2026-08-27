"""整理恢复租约字段的 Alembic 可逆迁移测试。"""

import importlib

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.db.models.transferpending import TransferPending

MIGRATION = "database.versions.d3a9e5f7b2c4_3_0_15"
LEASE_COLUMNS = {
    "lease_owner",
    "lease_token",
    "lease_expires_at",
    "heartbeat_at",
    "attempt_count",
}
TRANSFER_EXECUTION_COLUMNS = {
    "execution_state",
    "execution_version",
    "execution_payload",
    "execution_fingerprint",
    "retry_generation",
    "retry_count",
    "retry_due_at",
    "retry_requested_by",
    "retry_reason",
    "settlement_revision",
    "terminal_history_id",
    "manual_review_revision",
    "reviewed_at",
    "reviewed_by",
    "review_reason",
    "review_decision",
}


def _bind_migration(monkeypatch, connection):
    """把 3.0.15 迁移绑定到隔离数据库连接。"""
    migration = importlib.import_module(MIGRATION)
    monkeypatch.setattr(
        migration,
        "op",
        Operations(MigrationContext.configure(connection)),
    )
    return migration


def _create_planning_table(connection) -> None:
    """创建 3.0.14 时代包含完整规划检查点的登记表。"""
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
        sa.Column("input_version", sa.Integer(), nullable=False),
        sa.Column("planning_input", sa.JSON(), nullable=False),
        sa.Column("input_fingerprint", sa.String(64), nullable=False),
        sa.Column("checkpoint_version", sa.Integer(), nullable=True),
        sa.Column("checkpoint_payload", sa.JSON(), nullable=True),
        sa.Column("planned_at", sa.String(40), nullable=True),
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
    connection.execute(
        table.insert(),
        {
            "id": 1,
            "task_id": "stable-task",
            "storage": "local",
            "src_path": "/downloads/Movie.mkv",
            "created_at": "2026-08-27 10:00:00",
            "state": "planned",
            "updated_at": "2026-08-27 10:00:00",
            "last_error": None,
            "input_version": 1,
            "planning_input": {
                "schema_version": 1,
                "source_fileitem": {
                    "storage": "local",
                    "path": "/downloads/Movie.mkv",
                },
            },
            "input_fingerprint": "0" * 64,
            "checkpoint_version": 1,
            "checkpoint_payload": {"schema_version": 1},
            "planned_at": "2026-08-27 10:00:00",
        },
    )


def test_transfer_lease_upgrade_downgrade_reupgrade(monkeypatch) -> None:
    """SQLite 应支持租约字段重复升级、降级和再次升级。"""
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        _create_planning_table(connection)
        migration = _bind_migration(monkeypatch, connection)

        migration.upgrade()
        migration.upgrade()

        inspector = sa.inspect(connection)
        assert {
            column["name"]
            for column in inspector.get_columns("transferpending")
        } == {
            column.name
            for column in TransferPending.__table__.columns
            if column.name not in TRANSFER_EXECUTION_COLUMNS
        }
        assert "ix_transferpending_recovery_lease" in {
            index["name"]
            for index in inspector.get_indexes("transferpending")
        }
        row = connection.execute(
            sa.text(
                "SELECT lease_owner, lease_token, lease_expires_at, "
                "heartbeat_at, attempt_count FROM transferpending WHERE id = 1"
            )
        ).mappings().one()
        assert dict(row) == {
            "lease_owner": None,
            "lease_token": None,
            "lease_expires_at": None,
            "heartbeat_at": None,
            "attempt_count": 0,
        }

        connection.execute(
            sa.text(
                "UPDATE transferpending SET lease_owner = 'worker', "
                "lease_token = 'token', "
                "lease_expires_at = '2026-08-27 10:05:00.000000', "
                "heartbeat_at = '2026-08-27 10:00:00.000000', "
                "attempt_count = 3 WHERE id = 1"
            )
        )
        migration.downgrade()

        downgraded = sa.inspect(connection)
        assert LEASE_COLUMNS.isdisjoint({
            column["name"]
            for column in downgraded.get_columns("transferpending")
        })
        assert "ix_transferpending_recovery_lease" not in {
            index["name"]
            for index in downgraded.get_indexes("transferpending")
        }
        assert connection.execute(
            sa.text("SELECT state FROM transferpending WHERE id = 1")
        ).scalar_one() == "planned"

        migration.upgrade()
        reupgraded = connection.execute(
            sa.text(
                "SELECT lease_owner, lease_token, attempt_count "
                "FROM transferpending WHERE id = 1"
            )
        ).mappings().one()
        assert dict(reupgraded) == {
            "lease_owner": None,
            "lease_token": None,
            "attempt_count": 0,
        }
    engine.dispose()


def test_partial_transfer_lease_upgrade_preserves_existing_owner(
        monkeypatch,
) -> None:
    """迁移中断后重跑应补齐字段且不得覆盖已经写入的租约拥有者。"""
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        _create_planning_table(connection)
        connection.execute(
            sa.text("ALTER TABLE transferpending ADD COLUMN lease_owner VARCHAR(128)")
        )
        connection.execute(
            sa.text(
                "UPDATE transferpending SET lease_owner = 'preserved-worker' "
                "WHERE id = 1"
            )
        )
        migration = _bind_migration(monkeypatch, connection)

        migration.upgrade()

        row = connection.execute(
            sa.text(
                "SELECT lease_owner, lease_token, attempt_count "
                "FROM transferpending WHERE id = 1"
            )
        ).mappings().one()
        assert dict(row) == {
            "lease_owner": "preserved-worker",
            "lease_token": None,
            "attempt_count": 0,
        }
    engine.dispose()
