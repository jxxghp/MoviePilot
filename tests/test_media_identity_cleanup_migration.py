import importlib

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


LEGACY_COLUMNS = {
    "tmdbid", "imdbid", "tvdbid", "doubanid", "bangumiid",
    "anilistid", "mediaid",
}

PREFIXED_IDENTITIES = (
    ("tmdb", "themoviedb"),
    ("themoviedb", "themoviedb"),
    ("douban", "douban"),
    ("bangumi", "bangumi"),
    ("anilist", "anilist"),
    ("imdb", "imdb"),
    ("tvdb", "tvdb"),
    ("musicbrainz", "musicbrainz"),
    ("theaudiodb", "theaudiodb"),
    ("audio_db", "theaudiodb"),
    ("doubanmusic", "doubanmusic"),
    ("douban_music", "doubanmusic"),
    ("bilibili", "bilibili"),
    ("mangguodiscover", "mangguodiscover"),
    ("mango_tv", "mangguodiscover"),
    ("migu", "migu"),
    ("migu_video", "migu"),
    ("tencentvideodiscover", "tencentvideodiscover"),
    ("tencent_video", "tencentvideodiscover"),
)


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
    """创建媒体身份清理升级前六张通用媒体表的最小结构。"""
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
        "database.versions.8a4c7e1d2f90_3_0_2"
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
            {
                "id": 4,
                "media_source": "plugin_source",
                "media_id": "custom-1",
                "tmdbid": None,
                "doubanid": "35209731",
                "mediaid": None,
            },
            {
                "id": 5,
                "media_source": "themoviedb",
                "media_id": "0",
                "tmdbid": 0,
                "doubanid": "1295644",
                "mediaid": None,
            },
            {
                "id": 6,
                "media_source": " MusicBrainz ",
                "media_id": " release-group-1 ",
                "tmdbid": None,
                "doubanid": None,
                "mediaid": None,
            },
            {
                "id": 7,
                "media_source": None,
                "media_id": None,
                "tmdbid": None,
                "doubanid": None,
                "mediaid": "acme.video:custom-7",
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
        for table_name in migrated:
            constraint_names = {
                constraint["name"]
                for constraint in sa.inspect(connection).get_check_constraints(table_name)
            }
            assert f"ck_{table_name}_media_identity" in constraint_names

        assert (subscribe_rows[0]["media_source"], subscribe_rows[0]["media_id"]) == (
            "themoviedb", "550",
        )
        assert (subscribe_rows[1]["media_source"], subscribe_rows[1]["media_id"]) == (
            "anilist", "154587",
        )
        assert (subscribe_rows[2]["media_source"], subscribe_rows[2]["media_id"]) == (
            "themoviedb", "1396",
        )
        assert (subscribe_rows[3]["media_source"], subscribe_rows[3]["media_id"]) == (
            "plugin_source", "custom-1",
        )
        assert (subscribe_rows[4]["media_source"], subscribe_rows[4]["media_id"]) == (
            "douban", "1295644",
        )
        assert (subscribe_rows[5]["media_source"], subscribe_rows[5]["media_id"]) == (
            "musicbrainz", "release-group-1",
        )
        assert (subscribe_rows[6]["media_source"], subscribe_rows[6]["media_id"]) == (
            "acme.video", "custom-7",
        )
        assert identities["subscribehistory"]["media_source"] == "bangumi"
        assert identities["downloadhistory"]["media_source"] == "douban"
        assert identities["transferhistory"]["media_source"] == "anilist"
        assert identities["downloadfailure"]["media_source"] == "bangumi"
        assert identities["mediaserveritem"]["media_source"] == "themoviedb"


def test_cleanup_migration_backfills_every_supported_mediaid_prefix(monkeypatch) -> None:
    """升级应从历史组合字段回填全部规范来源及仍受支持的别名。"""
    migration = importlib.import_module(
        "database.versions.8a4c7e1d2f90_3_0_2"
    )
    engine = sa.create_engine("sqlite://")

    with engine.begin() as connection:
        tables = _create_tables(connection)
        connection.execute(
            tables["subscribe"].insert(),
            [
                {
                    "id": index,
                    "mediaid": f"{prefix}:native-{index}",
                }
                for index, (prefix, _) in enumerate(PREFIXED_IDENTITIES, start=1)
        ] + [{
            "id": len(PREFIXED_IDENTITIES) + 1,
            "mediaid": "audioXdb:must-not-match-alias",
            }],
        )

        monkeypatch.setattr(migration, "op", _operations(connection))
        migration.upgrade()
        migrated = sa.Table(
            "subscribe", sa.MetaData(), autoload_with=connection,
        )
        rows = connection.execute(
            sa.select(
                migrated.c.id,
                migrated.c.media_source,
                migrated.c.media_id,
            ).order_by(migrated.c.id)
        ).mappings().all()

    assert [
        (row["media_source"], row["media_id"])
        for row in rows[:-1]
    ] == [
        (source, f"native-{index}")
        for index, (_, source) in enumerate(PREFIXED_IDENTITIES, start=1)
    ]
    assert (rows[-1]["media_source"], rows[-1]["media_id"]) == (
        "audioxdb", "must-not-match-alias",
    )


def test_cleanup_migration_rejects_invalid_database_identity_pairs(monkeypatch) -> None:
    """升级后的数据库应允许插件来源，并拒绝半对、非法来源和零值身份。"""
    migration = importlib.import_module(
        "database.versions.8a4c7e1d2f90_3_0_2"
    )
    engine = sa.create_engine("sqlite://")

    with engine.begin() as connection:
        _create_tables(connection)
        monkeypatch.setattr(migration, "op", _operations(connection))
        migration.upgrade()
        subscribe = sa.Table(
            "subscribe", sa.MetaData(), autoload_with=connection,
        )
        for identity in (
            {"media_source": "themoviedb", "media_id": None},
            {"media_source": None, "media_id": "550"},
            {"media_source": "invalid:source", "media_id": "550"},
            {"media_source": "themoviedb", "media_id": "0"},
        ):
            with pytest.raises(sa.exc.IntegrityError):
                with connection.begin_nested():
                    connection.execute(subscribe.insert(), {
                        "name": "invalid",
                        **identity,
                    })

        connection.execute(subscribe.insert(), {
            "name": "plugin",
            "media_source": "plugin_source",
            "media_id": "custom-1",
        })

        connection.execute(subscribe.insert(), [
            {"name": "empty", "media_source": None, "media_id": None},
            {
                "name": "valid",
                "media_source": "musicbrainz",
                "media_id": "release-group-1",
            },
        ])


def test_cleanup_migration_downgrade_restores_legacy_schema(monkeypatch) -> None:
    """降级应恢复旧列并移除仅由本次迁移引入的媒体库身份列。"""
    migration = importlib.import_module(
        "database.versions.8a4c7e1d2f90_3_0_2"
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
