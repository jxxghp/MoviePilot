import importlib

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


def _create_legacy_tables(connection) -> dict[str, sa.Table]:
    """创建执行 2.2.14 迁移前的最小历史表结构。"""
    metadata = sa.MetaData()
    common_identity_columns = (
        sa.Column("tmdbid", sa.Integer()),
        sa.Column("doubanid", sa.String()),
    )
    tables = {
        "subscribe": sa.Table(
            "subscribe", metadata,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("mediaid", sa.String()),
            *common_identity_columns,
            sa.Column("bangumiid", sa.Integer()),
        ),
        "subscribehistory": sa.Table(
            "subscribehistory", metadata,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("mediaid", sa.String()),
            sa.Column("tmdbid", sa.Integer()),
            sa.Column("doubanid", sa.String()),
            sa.Column("bangumiid", sa.Integer()),
        ),
        "downloadhistory": sa.Table(
            "downloadhistory", metadata,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tmdbid", sa.Integer()),
            sa.Column("doubanid", sa.String()),
        ),
        "transferhistory": sa.Table(
            "transferhistory", metadata,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tmdbid", sa.Integer()),
            sa.Column("doubanid", sa.String()),
            sa.Column("media_source", sa.String()),
            sa.Column("media_id", sa.String()),
        ),
        "downloadfailure": sa.Table(
            "downloadfailure", metadata,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("type", sa.String()),
            sa.Column("site", sa.Integer()),
            sa.Column("tmdbid", sa.Integer()),
            sa.Column("doubanid", sa.String()),
        ),
    }
    metadata.create_all(connection)
    return tables


def test_media_identity_migration_adds_fields_and_backfills_rows(monkeypatch) -> None:
    """迁移应补齐五张表的媒体 ID 字段并幂等迁移存量身份。"""
    migration = importlib.import_module(
        "database.versions.f7b2d5c9a301_2_2_14"
    )
    engine = sa.create_engine("sqlite://")

    with engine.begin() as connection:
        tables = _create_legacy_tables(connection)
        connection.execute(tables["subscribe"].insert(), {
            "id": 1, "mediaid": "anilist:154587",
        })
        connection.execute(tables["subscribehistory"].insert(), {
            "id": 1, "bangumiid": 29648,
        })
        connection.execute(tables["downloadhistory"].insert(), {
            "id": 1, "doubanid": "35209731",
        })
        connection.execute(tables["transferhistory"].insert(), {
            "id": 1, "tmdbid": 209867,
            "media_source": "plugin_source", "media_id": "custom-1",
        })
        connection.execute(tables["downloadfailure"].insert(), {
            "id": 1, "type": "电视剧", "site": 1, "tmdbid": 209867,
        })

        context = MigrationContext.configure(connection)
        monkeypatch.setattr(migration, "op", Operations(context))
        migration.upgrade()
        migration.upgrade()

        migrated = {
            table_name: sa.Table(
                table_name, sa.MetaData(), autoload_with=connection,
            )
            for table_name in tables
        }
        rows = {
            table_name: connection.execute(
                sa.select(table).where(table.c.id == 1)
            ).mappings().one()
            for table_name, table in migrated.items()
        }

    for table_name in migrated:
        assert "bangumiid" in migrated[table_name].c
        assert "anilistid" in migrated[table_name].c
        assert "media_source" in migrated[table_name].c
        assert "media_id" in migrated[table_name].c
    assert rows["subscribe"]["media_source"] == "anilist"
    assert rows["subscribe"]["media_id"] == "154587"
    assert rows["subscribehistory"]["media_source"] == "bangumi"
    assert rows["subscribehistory"]["media_id"] == "29648"
    assert rows["downloadhistory"]["media_source"] == "douban"
    assert rows["downloadhistory"]["media_id"] == "35209731"
    assert rows["transferhistory"]["media_source"] == "plugin_source"
    assert rows["transferhistory"]["media_id"] == "custom-1"
    assert rows["downloadfailure"]["media_source"] == "themoviedb"
    assert rows["downloadfailure"]["media_id"] == "209867"
