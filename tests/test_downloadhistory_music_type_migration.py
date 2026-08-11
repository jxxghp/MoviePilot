import importlib

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


def test_downloadhistory_music_type_migration_backfills_music_notes(monkeypatch) -> None:
    """迁移应新增实体字段，并从版本化音乐备注中回填旧记录。"""
    migration = importlib.import_module(
        "database.versions.6f9a1c2d3e4b_3_0_1"
    )
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    download_history = sa.Table(
        "downloadhistory",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("note", sa.JSON()),
    )

    with engine.begin() as connection:
        metadata.create_all(connection)
        connection.execute(
            download_history.insert(),
            [
                {
                    "id": 1,
                    "note": {
                        "music": {
                            "version": 1,
                            "media": {"music_type": "album"},
                        }
                    },
                },
                {"id": 2, "note": {"source": "search"}},
            ],
        )
        context = MigrationContext.configure(connection)
        monkeypatch.setattr(migration, "op", Operations(context))

        migration.upgrade()
        migration.upgrade()

        migrated = sa.Table(
            "downloadhistory",
            sa.MetaData(),
            autoload_with=connection,
        )
        rows = connection.execute(
            sa.select(migrated).order_by(migrated.c.id)
        ).mappings().all()
        assert rows[0]["music_type"] == "album"
        assert rows[1]["music_type"] is None

        migration.downgrade()
        rolled_back = sa.Table(
            "downloadhistory",
            sa.MetaData(),
            autoload_with=connection,
        )
        assert "music_type" not in rolled_back.c
