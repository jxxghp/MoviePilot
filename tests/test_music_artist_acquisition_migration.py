"""艺术家作品获取任务的 SQLite 迁移一致性测试。"""

import importlib

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.db.models.musicartistacquisition import MusicArtistAcquisition

MIGRATION = "database.versions.c4a1e7d9b203_3_0_34"


def _bind_migration(monkeypatch, connection):
    migration = importlib.import_module(MIGRATION)
    monkeypatch.setattr(
        migration, "op", Operations(MigrationContext.configure(connection))
    )
    return migration


def test_artist_acquisition_migration_is_replay_safe(monkeypatch) -> None:
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        migration = _bind_migration(monkeypatch, connection)
        migration.upgrade()
        migration.upgrade()

        inspector = sa.inspect(connection)
        assert "musicartistacquisition" in inspector.get_table_names()
        assert {
            column["name"] for column in inspector.get_columns("musicartistacquisition")
        } == {column.name for column in MusicArtistAcquisition.__table__.columns}
        indexes = {
            item["name"]: item
            for item in inspector.get_indexes("musicartistacquisition")
        }
        assert indexes["ix_musicartistacquisition_job_id"]["unique"]
        assert indexes["ix_musicartistacquisition_plan_identity"]["unique"]

        migration.downgrade()
        migration.downgrade()
        assert "musicartistacquisition" not in sa.inspect(connection).get_table_names()
    engine.dispose()


def test_artist_acquisition_migration_accepts_create_all_table(monkeypatch) -> None:
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        MusicArtistAcquisition.__table__.create(connection)
        migration = _bind_migration(monkeypatch, connection)
        migration.upgrade()
        actual = {
            (item["name"], tuple(item["column_names"]), bool(item["unique"]))
            for item in sa.inspect(connection).get_indexes("musicartistacquisition")
        }
        expected = {
            (index.name, tuple(column.name for column in index.columns), index.unique)
            for index in MusicArtistAcquisition.__table__.indexes
        }
        assert actual == expected
    engine.dispose()
