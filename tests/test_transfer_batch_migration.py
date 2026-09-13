"""整理批次历史字段的 SQLite 迁移测试。"""

import importlib

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


def test_transfer_batch_migration_is_replay_safe(monkeypatch) -> None:
    """升级可重复执行，并在降级后只移除新增字段与索引。"""
    migration = importlib.import_module("database.versions.d6f4b2a9c813_3_0_35")
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    sa.Table(
        "transferhistory",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("src", sa.String()),
    )
    with engine.begin() as connection:
        metadata.create_all(connection)
        monkeypatch.setattr(
            migration,
            "op",
            Operations(MigrationContext.configure(connection)),
        )

        migration.upgrade()
        migration.upgrade()

        inspector = sa.inspect(connection)
        columns = {item["name"] for item in inspector.get_columns("transferhistory")}
        assert {
            "transfer_batch_id",
            "transfer_batch_title",
            "transfer_batch_root",
            "transfer_batch_total",
        }.issubset(columns)
        indexes = {item["name"] for item in inspector.get_indexes("transferhistory")}
        assert "ix_transferhistory_transfer_batch_id" in indexes

        migration.downgrade()
        migration.downgrade()
        remaining = {
            item["name"] for item in sa.inspect(connection).get_columns("transferhistory")
        }
        assert remaining == {"id", "src"}
    engine.dispose()
