"""历史表清理索引迁移测试。"""

import importlib

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


MIGRATION_MODULE = "database.versions.a6c8e2f4b1d3_3_0_11"
INDEXES = {
    "subscribehistory": (
        "ix_subscribehistory_date_id",
        ("date", "id"),
    ),
    "agentchat": (
        "ix_agentchat_updated_id",
        ("updated_at", "id"),
    ),
    "agenttaskrun": (
        "ix_agenttaskrun_status_started_id",
        ("status", "started_at", "id"),
    ),
}


def _index_columns(connection, table_name: str) -> dict[str, tuple[str, ...]]:
    """返回测试表的索引字段签名。"""
    return {
        index["name"]: tuple(index.get("column_names") or ())
        for index in sa.inspect(connection).get_indexes(table_name)
    }


def test_cleanup_index_migration_is_idempotent_and_reversible(monkeypatch) -> None:
    """升级可重复执行并创建准确索引，降级只移除新增索引。"""
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    sa.Table(
        "subscribehistory",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("date", sa.String()),
    )
    sa.Table(
        "agentchat",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("updated_at", sa.String()),
    )
    sa.Table(
        "agenttaskrun",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("status", sa.String()),
        sa.Column("started_at", sa.String()),
    )

    with engine.begin() as connection:
        metadata.create_all(connection)
        migration = importlib.import_module(MIGRATION_MODULE)
        monkeypatch.setattr(
            migration,
            "op",
            Operations(MigrationContext.configure(connection)),
        )

        migration.upgrade()
        migration.upgrade()
        for table_name, (index_name, columns) in INDEXES.items():
            assert _index_columns(connection, table_name)[index_name] == columns

        migration.downgrade()
        for table_name, (index_name, _columns) in INDEXES.items():
            assert index_name not in _index_columns(connection, table_name)
