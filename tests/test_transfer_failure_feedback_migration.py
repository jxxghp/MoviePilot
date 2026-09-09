"""整理失败反馈字段的数据库迁移测试。"""

import importlib

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

MIGRATION = "database.versions.a1b2c3d4e5f6_3_0_32"
EXPECTED_COLUMNS = {
    "retry_count",
    "auto_paused",
    "failure_stage",
    "recovery_action",
    "cleanup_status",
    "cleanup_error",
}


def _columns(connection) -> set[str]:
    """返回测试整理历史表的字段集合。"""
    return {
        column["name"]
        for column in sa.inspect(connection).get_columns("transferhistory")
    }


def test_transfer_failure_feedback_migration_is_idempotent_and_reversible(monkeypatch) -> None:
    """升级应幂等补齐失败闭环字段，降级应完整移除且保留原字段。"""
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    table = sa.Table(
        "transferhistory",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("src", sa.String()),
    )

    with engine.begin() as connection:
        metadata.create_all(connection)
        connection.execute(table.insert(), {"id": 1, "src": "/downloads/movie.mkv"})
        migration = importlib.import_module(MIGRATION)
        monkeypatch.setattr(
            migration,
            "op",
            Operations(MigrationContext.configure(connection)),
        )

        migration.upgrade()
        migration.upgrade()
        assert EXPECTED_COLUMNS <= _columns(connection)

        upgraded = sa.Table(
            "transferhistory",
            sa.MetaData(),
            autoload_with=connection,
        )
        row = connection.execute(
            sa.select(upgraded).where(upgraded.c.id == 1)
        ).mappings().one()
        assert row["auto_paused"] is False

        migration.downgrade()
        assert EXPECTED_COLUMNS.isdisjoint(_columns(connection))
        assert {"id", "src"} <= _columns(connection)
