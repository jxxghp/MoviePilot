import importlib
import os
import uuid

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

try:
    import psycopg2 as postgres_driver
    from psycopg2 import sql
    POSTGRESQL_SQLALCHEMY_DRIVER = "postgresql+psycopg2"
except ModuleNotFoundError:
    import psycopg as postgres_driver
    from psycopg import sql
    POSTGRESQL_SQLALCHEMY_DRIVER = "postgresql+psycopg"


E6_MIGRATION = "database.versions.e6a1c4b8d2f0_2_2_13"
F7_MIGRATION = "database.versions.f7b2d5c9a301_2_2_14"

IDENTITY_COLUMN_TYPES = {
    "mediaid": sa.String,
    "tmdbid": sa.Integer,
    "doubanid": sa.String,
    "bangumiid": sa.Integer,
    "anilistid": sa.Integer,
    "media_source": sa.String,
    "media_id": sa.String,
}
F7_INDEX_SIGNATURES = {
    "subscribe": {
        "ix_subscribe_anilistid": (("anilistid",), False),
        "ix_subscribe_media_source": (("media_source",), False),
        "ix_subscribe_media_id": (("media_id",), False),
        "ix_subscribe_media_identity": (
            ("media_source", "media_id"), False,
        ),
    },
    "subscribehistory": {
        "ix_subscribehistory_anilistid": (("anilistid",), False),
        "ix_subscribehistory_media_source": (("media_source",), False),
        "ix_subscribehistory_media_id": (("media_id",), False),
        "ix_subscribehistory_media_identity": (
            ("media_source", "media_id"), False,
        ),
    },
    "downloadhistory": {
        "ix_downloadhistory_bangumiid": (("bangumiid",), False),
        "ix_downloadhistory_anilistid": (("anilistid",), False),
        "ix_downloadhistory_media_source": (("media_source",), False),
        "ix_downloadhistory_media_id": (("media_id",), False),
        "ix_downloadhistory_media_identity": (
            ("media_source", "media_id"), False,
        ),
    },
    "transferhistory": {
        "ix_transferhistory_bangumiid": (("bangumiid",), False),
        "ix_transferhistory_anilistid": (("anilistid",), False),
        "ix_transferhistory_media_identity": (
            ("media_source", "media_id"), False,
        ),
    },
    "downloadfailure": {
        "ix_downloadfailure_media_identity_site": (
            ("type", "media_source", "media_id", "site"), False,
        ),
    },
}


def _bind_migration(monkeypatch, connection, module_name: str):
    """把历史 revision 绑定到当前 disposable connection。"""
    migration = importlib.import_module(module_name)
    context = MigrationContext.configure(connection)
    monkeypatch.setattr(migration, "op", Operations(context))
    return migration


def _column_names(connection, table_name: str) -> set[str]:
    """返回指定测试表的字段集合。"""
    return {
        column["name"]
        for column in sa.inspect(connection).get_columns(table_name)
    }


def _index_signatures(
        connection,
        table_name: str,
) -> dict[str, tuple[tuple[str, ...], bool]]:
    """返回索引名称到字段顺序及唯一性的映射。"""
    return {
        index["name"]: (
            tuple(index.get("column_names") or ()),
            bool(index.get("unique")),
        )
        for index in sa.inspect(connection).get_indexes(table_name)
    }


def _assert_index_signatures(
        actual: dict[str, tuple[tuple[str, ...], bool]],
        expected: dict[str, tuple[tuple[str, ...], bool]],
) -> None:
    """断言关键索引的字段顺序和唯一性均符合迁移契约。"""
    for index_name, signature in expected.items():
        assert actual.get(index_name) == signature, (index_name, actual)


def _rows(connection, table_name: str) -> list[dict]:
    """按主键读取迁移后的测试数据。"""
    table = sa.Table(table_name, sa.MetaData(), autoload_with=connection)
    return list(
        connection.execute(sa.select(table).order_by(table.c.id)).mappings()
    )


def _create_transferhistory(
        connection,
        columns: tuple[str, ...],
        indexes: tuple[tuple[str, tuple[str, ...]], ...] = (),
) -> sa.Table:
    """创建具有指定兼容字段和索引的最小整理历史表。"""
    metadata = sa.MetaData()
    table = sa.Table(
        "transferhistory",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        *(
            sa.Column(name, IDENTITY_COLUMN_TYPES[name]())
            for name in columns
        ),
    )
    for name, fields in indexes:
        sa.Index(name, *(table.c[field] for field in fields))
    metadata.create_all(connection)
    return table


def test_e6_backfill_preserves_priority_existing_identity_and_indexes(
        monkeypatch,
) -> None:
    """e6 应保持 TMDB 优先级、豆瓣兜底和已有规范身份。"""
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        table = _create_transferhistory(
            connection,
            ("tmdbid", "doubanid", "media_source", "media_id"),
            (("ix_transferhistory_media_source", ("media_source",)),),
        )
        connection.execute(table.insert(), [
            {
                "id": 1, "tmdbid": 123, "doubanid": "ignored",
                "media_source": None, "media_id": None,
            },
            {
                "id": 2, "tmdbid": None, "doubanid": "456",
                "media_source": None, "media_id": None,
            },
            {
                "id": 3,
                "tmdbid": 789,
                "doubanid": "also-ignored",
                "media_source": "plugin_source",
                "media_id": "custom-1",
            },
        ])
        migration = _bind_migration(monkeypatch, connection, E6_MIGRATION)

        migration.upgrade()
        migration.upgrade()

        migrated_rows = _rows(connection, "transferhistory")
        indexes = _index_signatures(connection, "transferhistory")

    assert (migrated_rows[0]["media_source"], migrated_rows[0]["media_id"]) == (
        "themoviedb", "123",
    )
    assert (migrated_rows[1]["media_source"], migrated_rows[1]["media_id"]) == (
        "douban", "456",
    )
    assert (migrated_rows[2]["media_source"], migrated_rows[2]["media_id"]) == (
        "plugin_source", "custom-1",
    )
    _assert_index_signatures(indexes, {
        "ix_transferhistory_media_source": (("media_source",), False),
        "ix_transferhistory_media_id": (("media_id",), False),
    })


@pytest.mark.parametrize(
    ("legacy_columns", "values", "expected_identity"),
    (
        (("tmdbid",), {"tmdbid": 123}, ("themoviedb", "123")),
        (("doubanid",), {"doubanid": "456"}, ("douban", "456")),
        ((), {}, (None, None)),
    ),
)
def test_e6_skips_only_missing_legacy_sources(
        monkeypatch,
        legacy_columns,
        values,
        expected_identity,
) -> None:
    """e6 应仅执行物理存在的旧来源字段回填。"""
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        table = _create_transferhistory(connection, legacy_columns)
        connection.execute(table.insert(), {"id": 1, **values})
        migration = _bind_migration(monkeypatch, connection, E6_MIGRATION)

        migration.upgrade()
        migration.upgrade()

        migrated = _rows(connection, "transferhistory")[0]

    assert (migrated["media_source"], migrated["media_id"]) == expected_identity


def test_e6_retry_completes_interrupted_column_and_index_state(
        monkeypatch,
) -> None:
    """e6 重试应独立补齐缺失的 revision-owned 字段和索引。"""
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        table = _create_transferhistory(
            connection,
            ("tmdbid", "doubanid", "media_source"),
            (("ix_transferhistory_media_source", ("media_source",)),),
        )
        connection.execute(
            table.insert(),
            {"id": 1, "tmdbid": None, "doubanid": "456"},
        )
        migration = _bind_migration(monkeypatch, connection, E6_MIGRATION)

        migration.upgrade()
        migration.upgrade()

        columns = _column_names(connection, "transferhistory")
        indexes = _index_signatures(connection, "transferhistory")
        migrated = _rows(connection, "transferhistory")[0]

    assert {"media_source", "media_id"}.issubset(columns)
    _assert_index_signatures(indexes, {
        "ix_transferhistory_media_source": (("media_source",), False),
        "ix_transferhistory_media_id": (("media_id",), False),
    })
    assert (migrated["media_source"], migrated["media_id"]) == (
        "douban", "456",
    )


def _create_f7_tables(
        connection,
        layouts: dict[str, tuple[str, ...]],
        indexes: dict[str, tuple[tuple[str, tuple[str, ...]], ...]] | None = None,
        transferhistory_identity: bool = True,
) -> dict[str, sa.Table]:
    """创建五张具有不同历史字段状态的最小媒体表。"""
    metadata = sa.MetaData()
    indexes = indexes or {}
    transferhistory_columns = (
        ("media_source", "media_id") if transferhistory_identity else ()
    )
    base_columns = {
        "subscribe": (),
        "subscribehistory": (),
        "downloadhistory": (),
        "transferhistory": transferhistory_columns,
        "downloadfailure": (),
    }
    tables = {}
    for table_name, required_columns in base_columns.items():
        column_names = tuple(dict.fromkeys(required_columns + layouts.get(table_name, ())))
        columns = [sa.Column("id", sa.Integer(), primary_key=True)]
        if table_name == "downloadfailure":
            columns.extend((
                sa.Column("type", sa.String()),
                sa.Column("site", sa.Integer()),
            ))
        columns.extend(
            sa.Column(name, IDENTITY_COLUMN_TYPES[name]())
            for name in column_names
        )
        table = sa.Table(table_name, metadata, *columns)
        for index_name, fields in indexes.get(table_name, ()):
            sa.Index(index_name, *(table.c[field] for field in fields))
        tables[table_name] = table
    metadata.create_all(connection)
    return tables


def test_f7_backfill_uses_physical_columns_and_preserves_priority(
        monkeypatch,
) -> None:
    """f7 应先用带前缀 ID，再按四类来源字段顺序回填。"""
    engine = sa.create_engine("sqlite://")
    layouts = {
        "subscribe": (
            "mediaid", "tmdbid", "doubanid", "bangumiid", "anilistid",
            "media_source", "media_id",
        ),
        "subscribehistory": ("mediaid",),
        "downloadhistory": ("doubanid",),
        "transferhistory": ("tmdbid",),
        "downloadfailure": (),
    }
    with engine.begin() as connection:
        tables = _create_f7_tables(connection, layouts)
        connection.execute(tables["subscribe"].insert(), [
            {
                "id": 1, "mediaid": "douban:prefix-1", "tmdbid": 101,
                "doubanid": "201", "bangumiid": 301, "anilistid": 401,
                "media_source": None, "media_id": None,
            },
            {
                "id": 2, "mediaid": None, "tmdbid": 102, "doubanid": "202",
                "bangumiid": 302, "anilistid": 402,
                "media_source": None, "media_id": None,
            },
            {
                "id": 3, "mediaid": None, "tmdbid": None,
                "doubanid": "203", "bangumiid": 303,
                "anilistid": 403,
                "media_source": None, "media_id": None,
            },
            {
                "id": 4, "mediaid": None, "tmdbid": None,
                "doubanid": None, "bangumiid": 304, "anilistid": 404,
                "media_source": None, "media_id": None,
            },
            {
                "id": 5, "mediaid": None, "tmdbid": None,
                "doubanid": None, "bangumiid": None, "anilistid": 405,
                "media_source": None, "media_id": None,
            },
            {
                "id": 6, "mediaid": "tmdb:106", "tmdbid": 106,
                "doubanid": None, "bangumiid": None, "anilistid": None,
                "media_source": "plugin_source", "media_id": "custom-6",
            },
        ])
        connection.execute(
            tables["subscribehistory"].insert(),
            {"id": 1, "mediaid": "anilist:154587"},
        )
        connection.execute(
            tables["downloadhistory"].insert(),
            {"id": 1, "doubanid": "35209731"},
        )
        connection.execute(
            tables["transferhistory"].insert(),
            {"id": 1, "tmdbid": 209867},
        )
        connection.execute(tables["downloadfailure"].insert(), {
            "id": 1, "type": "电视剧", "site": 1,
        })
        migration = _bind_migration(monkeypatch, connection, F7_MIGRATION)

        migration.upgrade()
        migration.upgrade()

        subscribe_rows = _rows(connection, "subscribe")
        partial_rows = {
            name: _rows(connection, name)[0]
            for name in (
                "subscribehistory", "downloadhistory", "transferhistory",
                "downloadfailure",
            )
        }

    assert [
        (row["media_source"], row["media_id"])
        for row in subscribe_rows
    ] == [
        ("douban", "prefix-1"),
        ("themoviedb", "102"),
        ("douban", "203"),
        ("bangumi", "304"),
        ("anilist", "405"),
        ("plugin_source", "custom-6"),
    ]
    assert (
        partial_rows["subscribehistory"]["media_source"],
        partial_rows["subscribehistory"]["media_id"],
    ) == ("anilist", "154587")
    assert (
        partial_rows["downloadhistory"]["media_source"],
        partial_rows["downloadhistory"]["media_id"],
    ) == ("douban", "35209731")
    assert (
        partial_rows["transferhistory"]["media_source"],
        partial_rows["transferhistory"]["media_id"],
    ) == ("themoviedb", "209867")
    assert partial_rows["downloadfailure"]["media_id"] is None


def test_f7_retry_completes_mixed_partial_table_states(monkeypatch) -> None:
    """f7 重试应跨表补齐混合缺失的字段和 revision-owned 索引。"""
    engine = sa.create_engine("sqlite://")
    layouts = {
        "subscribe": (
            "mediaid", "tmdbid", "anilistid", "media_source", "media_id",
        ),
        "subscribehistory": ("mediaid", "doubanid", "media_source"),
        "downloadhistory": ("bangumiid", "media_id"),
        "transferhistory": ("tmdbid", "bangumiid"),
        "downloadfailure": ("anilistid", "media_source", "media_id"),
    }
    existing_indexes = {
        "subscribe": (("ix_subscribe_anilistid", ("anilistid",)),),
        "subscribehistory": (
            ("ix_subscribehistory_media_source", ("media_source",)),
        ),
        "downloadhistory": (
            ("ix_downloadhistory_bangumiid", ("bangumiid",)),
        ),
        "transferhistory": (
            (
                "ix_transferhistory_media_identity",
                ("media_source", "media_id"),
            ),
        ),
        "downloadfailure": (
            (
                "ix_downloadfailure_media_identity_site",
                ("type", "media_source", "media_id", "site"),
            ),
        ),
    }
    with engine.begin() as connection:
        tables = _create_f7_tables(connection, layouts, existing_indexes)
        connection.execute(tables["subscribe"].insert(), {
            "id": 1,
            "mediaid": "douban:88",
            "tmdbid": 11,
            "media_source": "plugin_source",
            "media_id": "preserved-1",
        })
        connection.execute(tables["subscribehistory"].insert(), {
            "id": 1, "mediaid": "tmdb:22", "doubanid": "33",
        })
        connection.execute(tables["downloadhistory"].insert(), [
            {"id": 1, "bangumiid": 44, "media_id": None},
            {"id": 2, "bangumiid": 45, "media_id": "preserved-2"},
        ])
        connection.execute(
            tables["transferhistory"].insert(),
            {"id": 1, "tmdbid": 55, "bangumiid": 56},
        )
        connection.execute(tables["downloadfailure"].insert(), {
            "id": 1, "type": "动画", "site": 1, "anilistid": 66,
        })
        migration = _bind_migration(monkeypatch, connection, F7_MIGRATION)

        migration.upgrade()
        migration.upgrade()

        columns = {
            name: _column_names(connection, name)
            for name in tables
        }
        indexes = {
            name: _index_signatures(connection, name)
            for name in tables
        }
        rows = {name: _rows(connection, name) for name in tables}

    assert {"anilistid", "media_source", "media_id"}.issubset(columns["subscribe"])
    assert {"anilistid", "media_source", "media_id"}.issubset(
        columns["subscribehistory"]
    )
    assert {
        "bangumiid", "anilistid", "media_source", "media_id",
    }.issubset(columns["downloadhistory"])
    assert {"bangumiid", "anilistid"}.issubset(columns["transferhistory"])
    assert {
        "bangumiid", "anilistid", "media_source", "media_id",
    }.issubset(columns["downloadfailure"])

    for table_name, expected in F7_INDEX_SIGNATURES.items():
        _assert_index_signatures(indexes[table_name], expected)

    assert (rows["subscribe"][0]["media_source"], rows["subscribe"][0]["media_id"]) == (
        "plugin_source", "preserved-1",
    )
    assert (
        rows["subscribehistory"][0]["media_source"],
        rows["subscribehistory"][0]["media_id"],
    ) == ("themoviedb", "22")
    assert (
        rows["downloadhistory"][0]["media_source"],
        rows["downloadhistory"][0]["media_id"],
    ) == ("bangumi", "44")
    assert rows["downloadhistory"][1]["media_source"] is None
    assert rows["downloadhistory"][1]["media_id"] == "preserved-2"
    assert (
        rows["transferhistory"][0]["media_source"],
        rows["transferhistory"][0]["media_id"],
    ) == ("themoviedb", "55")
    assert (
        rows["downloadfailure"][0]["media_source"],
        rows["downloadfailure"][0]["media_id"],
    ) == ("anilist", "66")


def _exercise_e6_f7_round_trip(monkeypatch, engine: sa.Engine) -> None:
    """验证两个历史 revision 可升级、降级并再次升级。"""
    layouts = {
        "subscribe": ("mediaid", "tmdbid", "doubanid", "bangumiid"),
        "subscribehistory": ("mediaid", "tmdbid", "doubanid", "bangumiid"),
        "downloadhistory": ("tmdbid", "doubanid"),
        "transferhistory": ("tmdbid", "doubanid"),
        "downloadfailure": ("tmdbid", "doubanid"),
    }
    with engine.begin() as connection:
        tables = _create_f7_tables(
            connection,
            layouts,
            transferhistory_identity=False,
        )
        connection.execute(tables["subscribe"].insert(), {
            "id": 1, "mediaid": "douban:88", "tmdbid": 11,
        })
        connection.execute(tables["subscribehistory"].insert(), {
            "id": 1, "mediaid": None, "tmdbid": 22,
        })
        connection.execute(tables["downloadhistory"].insert(), {
            "id": 1, "tmdbid": None, "doubanid": "33",
        })
        connection.execute(tables["transferhistory"].insert(), {
            "id": 1, "tmdbid": 55, "doubanid": "ignored",
        })
        connection.execute(tables["downloadfailure"].insert(), {
            "id": 1, "type": "动画", "site": 1,
            "tmdbid": None, "doubanid": "66",
        })

        e6_migration = _bind_migration(monkeypatch, connection, E6_MIGRATION)
        e6_migration.upgrade()
        _assert_index_signatures(
            _index_signatures(connection, "transferhistory"),
            {
                "ix_transferhistory_media_source": (
                    ("media_source",), False,
                ),
                "ix_transferhistory_media_id": (("media_id",), False),
            },
        )
        e6_migration.downgrade()
        assert "media_source" not in _column_names(connection, "transferhistory")
        assert "media_id" not in _column_names(connection, "transferhistory")
        assert "ix_transferhistory_media_source" not in _index_signatures(
            connection, "transferhistory"
        )
        e6_migration.upgrade()
        transfer_row = _rows(connection, "transferhistory")[0]
        assert (transfer_row["media_source"], transfer_row["media_id"]) == (
            "themoviedb", "55",
        )

        pre_f7_columns = {
            "subscribe": {
                "id", "mediaid", "tmdbid", "doubanid", "bangumiid",
            },
            "subscribehistory": {
                "id", "mediaid", "tmdbid", "doubanid", "bangumiid",
            },
            "downloadhistory": {"id", "tmdbid", "doubanid"},
            "transferhistory": {
                "id", "tmdbid", "doubanid", "media_source", "media_id",
            },
            "downloadfailure": {
                "id", "type", "site", "tmdbid", "doubanid",
            },
        }
        for table_name, expected_columns in pre_f7_columns.items():
            assert _column_names(connection, table_name) == expected_columns

        f7_migration = _bind_migration(monkeypatch, connection, F7_MIGRATION)
        f7_migration.upgrade()
        for table_name, expected in F7_INDEX_SIGNATURES.items():
            _assert_index_signatures(
                _index_signatures(connection, table_name),
                expected,
            )
        f7_migration.downgrade()
        for table_name, expected_columns in pre_f7_columns.items():
            assert _column_names(connection, table_name) == expected_columns
        for table_name, expected in F7_INDEX_SIGNATURES.items():
            indexes = _index_signatures(connection, table_name)
            assert set(expected).isdisjoint(indexes), (table_name, indexes)

        f7_migration.upgrade()
        for table_name, expected in F7_INDEX_SIGNATURES.items():
            _assert_index_signatures(
                _index_signatures(connection, table_name),
                expected,
            )
        rows = {
            table_name: _rows(connection, table_name)[0]
            for table_name in layouts
        }

    assert (rows["subscribe"]["media_source"], rows["subscribe"]["media_id"]) == (
        "douban", "88",
    )
    assert (
        rows["subscribehistory"]["media_source"],
        rows["subscribehistory"]["media_id"],
    ) == ("themoviedb", "22")
    assert (
        rows["downloadhistory"]["media_source"],
        rows["downloadhistory"]["media_id"],
    ) == ("douban", "33")
    assert (
        rows["transferhistory"]["media_source"],
        rows["transferhistory"]["media_id"],
    ) == ("themoviedb", "55")
    assert (
        rows["downloadfailure"]["media_source"],
        rows["downloadfailure"]["media_id"],
    ) == ("douban", "66")


def test_e6_f7_round_trip_on_sqlite(monkeypatch) -> None:
    """SQLite 应支持两个 revision 的 upgrade/downgrade/re-upgrade。"""
    _exercise_e6_f7_round_trip(monkeypatch, sa.create_engine("sqlite://"))


def test_e6_f7_round_trip_on_postgresql(monkeypatch) -> None:
    """已配置的 PostgreSQL 15 应执行与 SQLite 相同的往返路径。"""
    prefix = "MOVIEPILOT_TEST_POSTGRESQL_"
    host = os.getenv(f"{prefix}HOST")
    database = os.getenv(f"{prefix}DATABASE")
    username = os.getenv(f"{prefix}USERNAME")
    if not host or not database or not username:
        pytest.skip("未配置隔离 PostgreSQL migration 测试库")

    port = os.getenv(f"{prefix}PORT", "5432")
    password = os.getenv(f"{prefix}PASSWORD", "")
    schema = f"p1_db1_roundtrip_{uuid.uuid4().hex}"
    with postgres_driver.connect(
        host=host,
        port=port,
        dbname=database,
        user=username,
        password=password,
    ) as connection:
        assert connection.server_version // 10000 == 15, connection.server_version
        connection.autocommit = True
        with connection.cursor() as cursor:
            cursor.execute(
                sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema))
            )

    engine = None
    try:
        engine = sa.create_engine(
            sa.URL.create(
                POSTGRESQL_SQLALCHEMY_DRIVER,
                username=username,
                password=password,
                host=host,
                port=int(port),
                database=database,
            ),
            connect_args={"options": f"-csearch_path={schema}"},
        )
        _exercise_e6_f7_round_trip(monkeypatch, engine)
    finally:
        if engine is not None:
            engine.dispose()
        with postgres_driver.connect(
            host=host,
            port=port,
            dbname=database,
            user=username,
            password=password,
        ) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                cursor.execute(
                    sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                        sql.Identifier(schema)
                    )
                )
