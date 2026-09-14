"""专辑订阅音轨事实列迁移测试。"""

import importlib

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

MIGRATION_MODULE = "database.versions.c8e4a1f7b2d6_3_0_39"


def test_music_subscription_tracks_migration_is_idempotent_and_reversible(monkeypatch) -> None:
    """迁移应为既有订阅表幂等增加 JSON 列，并可安全回退。"""
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    sa.Table(
        "subscribe",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String()),
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
        columns = {column["name"] for column in sa.inspect(connection).get_columns("subscribe")}
        assert "downloaded_tracks" in columns

        migration.downgrade()
        columns = {column["name"] for column in sa.inspect(connection).get_columns("subscribe")}
        assert "downloaded_tracks" not in columns


def test_music_subscription_tracks_migration_ignores_missing_table(monkeypatch) -> None:
    """基础表尚未创建时，迁移应等待初始化建表而不是失败。"""
    engine = sa.create_engine("sqlite://")

    with engine.begin() as connection:
        migration = importlib.import_module(MIGRATION_MODULE)
        monkeypatch.setattr(
            migration,
            "op",
            Operations(MigrationContext.configure(connection)),
        )

        migration.upgrade()
        migration.downgrade()
