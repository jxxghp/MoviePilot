import importlib

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


LEGACY_COLUMNS = {
    "tmdbid", "imdbid", "tvdbid", "doubanid", "bangumiid",
    "anilistid", "mediaid",
}


def _legacy_identity_columns(include_mediaid: bool = False) -> list[sa.Column]:
    """构造升级前来源专用媒体身份字段。"""
    columns = [
        sa.Column("tmdbid", sa.Integer()),
        sa.Column("imdbid", sa.String()),
        sa.Column("tvdbid", sa.Integer()),
        sa.Column("doubanid", sa.String()),
        sa.Column("bangumiid", sa.Integer()),
        sa.Column("anilistid", sa.Integer()),
    ]
    if include_mediaid:
        columns.append(sa.Column("mediaid", sa.String()))
    return columns


def _create_tables(connection) -> dict[str, sa.Table]:
    """创建 3.0.1 升级前六张通用媒体表的最小结构。"""
    metadata = sa.MetaData()
    tables = {
        "subscribe": sa.Table(
            "subscribe", metadata,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("media_source", sa.String()),
            sa.Column("media_id", sa.String()),
            *_legacy_identity_columns(include_mediaid=True),
        ),
        "subscribehistory": sa.Table(
            "subscribehistory", metadata,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("media_source", sa.String()),
            sa.Column("media_id", sa.String()),
            *_legacy_identity_columns(include_mediaid=True),
        ),
        "downloadhistory": sa.Table(
            "downloadhistory", metadata,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("media_source", sa.String()),
            sa.Column("media_id", sa.String()),
            *_legacy_identity_columns(),
        ),
        "transferhistory": sa.Table(
            "transferhistory", metadata,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("media_source", sa.String()),
            sa.Column("media_id", sa.String()),
            *_legacy_identity_columns(),
        ),
        "downloadfailure": sa.Table(
            "downloadfailure", metadata,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("type", sa.String()),
            sa.Column("site", sa.Integer()),
            sa.Column("media_source", sa.String()),
            sa.Column("media_id", sa.String()),
            sa.Column("tmdbid", sa.Integer()),
            sa.Column("doubanid", sa.String()),
            sa.Column("bangumiid", sa.Integer()),
            sa.Column("anilistid", sa.Integer()),
        ),
        "mediaserveritem": sa.Table(
            "mediaserveritem", metadata,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("item_type", sa.String()),
            sa.Column("tmdbid", sa.Integer()),
            sa.Column("imdbid", sa.String()),
            sa.Column("tvdbid", sa.Integer()),
        ),
    }
    metadata.create_all(connection)
    return tables


def _operations(connection) -> Operations:
    """创建绑定当前连接的 Alembic Operations。"""
    return Operations(MigrationContext.configure(connection))


def test_cleanup_migration_keeps_one_complete_identity_and_drops_legacy_columns(
        monkeypatch,
) -> None:
    """升级应修复半对身份、保留完整身份并删除六表全部专用列。"""
    migration = importlib.import_module(
        "database.versions.8a4c7e1d2f90_3_0_1"
    )
    engine = sa.create_engine("sqlite://")

    with engine.begin() as connection:
        tables = _create_tables(connection)
        connection.execute(tables["subscribe"].insert(), [
            {
                "id": 1,
                "media_source": "tmdb",
                "media_id": "550",
                "tmdbid": 999,
                "mediaid": None,
                "doubanid": None,
            },
            {
                "id": 2,
                "media_source": None,
                "media_id": "stale-partial",
                "mediaid": "anilist:154587",
                "tmdbid": None,
                "doubanid": None,
            },
            {
                "id": 3,
                "media_source": None,
                "media_id": None,
                "mediaid": None,
                "tmdbid": 1396,
                "doubanid": "999999",
            },
        ])
        connection.execute(tables["subscribehistory"].insert(), {
            "id": 1, "mediaid": "bangumi:400602",
        })
        connection.execute(tables["downloadhistory"].insert(), {
            "id": 1, "doubanid": "35209731",
        })
        connection.execute(tables["transferhistory"].insert(), {
            "id": 1, "anilistid": 154587,
        })
        connection.execute(tables["downloadfailure"].insert(), {
            "id": 1, "type": "电视剧", "site": 1, "bangumiid": 400602,
        })
        connection.execute(tables["mediaserveritem"].insert(), {
            "id": 1, "item_type": "Movie", "tmdbid": 550, "imdbid": "tt0137523",
        })

        monkeypatch.setattr(migration, "op", _operations(connection))
        migration.upgrade()
        migration.upgrade()

        migrated = {
            name: sa.Table(name, sa.MetaData(), autoload_with=connection)
            for name in tables
        }
        subscribe_rows = connection.execute(
            sa.select(migrated["subscribe"]).order_by(migrated["subscribe"].c.id)
        ).mappings().all()
        identities = {
            name: connection.execute(sa.select(table)).mappings().first()
            for name, table in migrated.items()
            if name != "subscribe"
        }

        for table in migrated.values():
            assert {"media_source", "media_id"}.issubset(table.c.keys())
            assert LEGACY_COLUMNS.isdisjoint(table.c.keys())

        assert (subscribe_rows[0]["media_source"], subscribe_rows[0]["media_id"]) == (
            "themoviedb", "550",
        )
        assert (subscribe_rows[1]["media_source"], subscribe_rows[1]["media_id"]) == (
            "anilist", "154587",
        )
        assert (subscribe_rows[2]["media_source"], subscribe_rows[2]["media_id"]) == (
            "themoviedb", "1396",
        )
        assert identities["subscribehistory"]["media_source"] == "bangumi"
        assert identities["downloadhistory"]["media_source"] == "douban"
        assert identities["transferhistory"]["media_source"] == "anilist"
        assert identities["downloadfailure"]["media_source"] == "bangumi"
        assert identities["mediaserveritem"]["media_source"] == "themoviedb"


def test_cleanup_migration_downgrade_restores_legacy_schema(monkeypatch) -> None:
    """降级应恢复旧列并移除仅由本次迁移引入的媒体库身份列。"""
    migration = importlib.import_module(
        "database.versions.8a4c7e1d2f90_3_0_1"
    )
    engine = sa.create_engine("sqlite://")

    with engine.begin() as connection:
        _create_tables(connection)
        monkeypatch.setattr(migration, "op", _operations(connection))
        migration.upgrade()
        migration.downgrade()

        inspector = sa.inspect(connection)
        media_server_columns = {
            column["name"]
            for column in inspector.get_columns("mediaserveritem")
        }
        subscribe_columns = {
            column["name"]
            for column in inspector.get_columns("subscribe")
        }

    assert {"tmdbid", "imdbid", "tvdbid"}.issubset(media_server_columns)
    assert {"media_source", "media_id"}.isdisjoint(media_server_columns)
    assert LEGACY_COLUMNS.issubset(subscribe_columns)
    assert {"media_source", "media_id"}.issubset(subscribe_columns)
