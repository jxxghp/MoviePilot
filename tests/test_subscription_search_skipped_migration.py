"""订阅搜索批次跳过计数的 Alembic 可逆迁移测试。"""

import importlib

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

MIGRATION = "database.versions.b6c1d9e4a7f2_3_0_24"


def _bind_migration(monkeypatch, connection):
    """把跳过计数迁移绑定到隔离 SQLite 连接。"""
    migration = importlib.import_module(MIGRATION)
    monkeypatch.setattr(
        migration,
        "op",
        Operations(MigrationContext.configure(connection)),
    )
    return migration


def test_subscription_search_skipped_count_migration_is_reversible(monkeypatch) -> None:
    """存量批次获得零默认值，迁移可重复执行并完整回滚。"""
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    batch = sa.Table(
        "subscriptionsearchbatch",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("batch_id", sa.String(64), nullable=False),
    )
    with engine.begin() as connection:
        metadata.create_all(connection)
        connection.execute(batch.insert(), {"id": 1, "batch_id": "batch-1"})
        migration = _bind_migration(monkeypatch, connection)

        migration.upgrade()
        migration.upgrade()

        columns = {
            column["name"]
            for column in sa.inspect(connection).get_columns("subscriptionsearchbatch")
        }
        assert "skipped_count" in columns
        assert connection.execute(
            sa.text("SELECT skipped_count FROM subscriptionsearchbatch WHERE id = 1")
        ).scalar_one() == 0

        migration.downgrade()
        downgraded = {
            column["name"]
            for column in sa.inspect(connection).get_columns("subscriptionsearchbatch")
        }
        assert "skipped_count" not in downgraded

        migration.upgrade()
        assert connection.execute(
            sa.text("SELECT skipped_count FROM subscriptionsearchbatch WHERE id = 1")
        ).scalar_one() == 0
    engine.dispose()
