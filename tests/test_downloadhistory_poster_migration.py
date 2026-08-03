import importlib

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


def test_downloadhistory_poster_migration_adds_and_removes_column(monkeypatch) -> None:
    """迁移应为下载历史增加可空海报字段，并支持回滚。"""
    migration = importlib.import_module(
        "database.versions.a8c4e2f6b1d9_2_2_15"
    )
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    download_history = sa.Table(
        "downloadhistory",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("image", sa.String()),
    )

    with engine.begin() as connection:
        metadata.create_all(connection)
        connection.execute(download_history.insert(), {
            "id": 1,
            "image": "https://images.example.com/backdrop.jpg",
        })
        context = MigrationContext.configure(connection)
        monkeypatch.setattr(migration, "op", Operations(context))

        migration.upgrade()
        migration.upgrade()

        migrated = sa.Table(
            "downloadhistory",
            sa.MetaData(),
            autoload_with=connection,
        )
        row = connection.execute(sa.select(migrated)).mappings().one()
        assert "poster" in migrated.c
        assert row["image"] == "https://images.example.com/backdrop.jpg"
        assert row["poster"] is None

        migration.downgrade()
        rolled_back = sa.Table(
            "downloadhistory",
            sa.MetaData(),
            autoload_with=connection,
        )
        assert "poster" not in rolled_back.c
