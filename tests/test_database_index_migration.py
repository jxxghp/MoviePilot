import importlib
import os
from pathlib import Path
import subprocess
import sys
import uuid

import psycopg2
from psycopg2 import sql
import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


MIGRATION_MODULE = "database.versions.93f8cb6a4d1e_2_2_4"
MEDIA_TABLES = (
    "subscribe",
    "subscribehistory",
    "downloadhistory",
    "transferhistory",
    "downloadfailure",
    "mediaserveritem",
)
LEGACY_IDENTITY_COLUMNS = {
    "tmdbid",
    "imdbid",
    "tvdbid",
    "doubanid",
    "bangumiid",
    "anilistid",
    "mediaid",
}
IDENTITY_INDEX_SIGNATURES = {
    "subscribe": {
        "ix_subscribe_media_identity": (("media_source", "media_id"), False),
    },
    "subscribehistory": {
        "ix_subscribehistory_media_identity": (
            ("media_source", "media_id"), False,
        ),
    },
    "downloadhistory": {
        "ix_downloadhistory_media_identity": (
            ("media_source", "media_id"), False,
        ),
    },
    "transferhistory": {
        "ix_transferhistory_media_identity": (
            ("media_source", "media_id"), False,
        ),
    },
    "downloadfailure": {
        "ix_downloadfailure_media_identity_site": (
            ("type", "media_source", "media_id", "site"), False,
        ),
    },
    "mediaserveritem": {
        "ix_mediaserveritem_media_identity_type": (
            ("media_source", "media_id", "item_type"), False,
        ),
    },
}


CURRENT_SCHEMA_CHAIN_SCRIPT = """
from app.testing.bootstrap import ensure_sites_stub

# Alembic 会导入引用业务链的旧 revision；全新 CI 环境没有动态下发的 sites 模块。
ensure_sites_stub()

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from app.runtime.config import settings
from app.db import get_engine
from app.startup.database_initializer import init_db, update_db

media_tables = {media_tables!r}
legacy_identity_columns = {legacy_identity_columns!r}
identity_index_signatures = {identity_index_signatures!r}

config = Config()
config.set_main_option("script_location", str(settings.ROOT_PATH / "database"))
heads = ScriptDirectory.from_config(config).get_heads()
assert len(heads) == 1, heads

init_db()
update_db()
update_db()

with get_engine().connect() as connection:
    version = connection.execute(
        text("SELECT version_num FROM alembic_version")
    ).scalar_one()
    inspector = inspect(connection)
    assert version == heads[0], (version, heads)

    for table_name in media_tables:
        columns = {{
            column["name"]
            for column in inspector.get_columns(table_name)
        }}
        indexes = {{
            index["name"]: (
                tuple(index.get("column_names") or ()),
                bool(index.get("unique")),
            )
            for index in inspector.get_indexes(table_name)
        }}
        constraints = {{
            constraint["name"]: constraint.get("sqltext") or ""
            for constraint in inspector.get_check_constraints(table_name)
        }}
        assert {{"media_source", "media_id"}}.issubset(columns), (
            table_name,
            columns,
        )
        assert legacy_identity_columns.isdisjoint(columns), (
            table_name,
            columns,
        )
        for index_name, signature in identity_index_signatures[table_name].items():
            assert indexes.get(index_name) == signature, (
                table_name,
                index_name,
                indexes,
            )
        constraint_name = f"ck_{{table_name}}_media_identity"
        assert constraint_name in constraints, (
            table_name,
            constraints,
        )
        normalized_sql = "".join(
            constraints[constraint_name].lower().replace('"', '').split()
        )
        for text_cast in ("::text[]", "::text", "::charactervarying"):
            normalized_sql = normalized_sql.replace(text_cast, "")
        for fragment in (
            "media_sourceisnull",
            "media_idisnull",
            "media_sourceisnotnull",
            "media_idisnotnull",
            "length(media_source)",
            "media_sourcenotlike'%:%'",
        ):
            assert fragment in normalized_sql, (
                table_name,
                constraints[constraint_name],
            )
        assert any(
            trim_form in normalized_sql
            for trim_form in (
                "trim(media_id)",
                "trim(bothfrommedia_id)",
            )
        ), (table_name, constraints[constraint_name])
        assert "<>''" in normalized_sql, (
            table_name,
            constraints[constraint_name],
        )
        assert "<>'0'" in normalized_sql, (
            table_name,
            constraints[constraint_name],
        )

    constraint_name = "ck_mediaserveritem_media_identity"
    try:
        with connection.begin_nested():
            connection.execute(
                text(
                    "INSERT INTO mediaserveritem (media_source, media_id) "
                    "VALUES (:media_source, :media_id)"
                ),
                {{"media_source": "invalid:source", "media_id": "1"}},
            )
    except IntegrityError as error:
        assert constraint_name in str(error.orig), str(error.orig)
    else:
        raise AssertionError("格式非法的媒体身份未被具名检查约束拒绝")
""".format(
    media_tables=MEDIA_TABLES,
    legacy_identity_columns=LEGACY_IDENTITY_COLUMNS,
    identity_index_signatures=IDENTITY_INDEX_SIGNATURES,
)


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


def _bind_migration(monkeypatch, connection):
    """把历史 revision 绑定到当前 disposable connection。"""
    migration = importlib.import_module(MIGRATION_MODULE)
    context = MigrationContext.configure(connection)
    monkeypatch.setattr(migration, "op", Operations(context))
    return migration


def _run_current_schema_chain(
        repository: Path,
        environment: dict[str, str],
) -> None:
    """在隔离数据库中执行当前建表、完整升级及最终结构断言。"""
    completed = subprocess.run(
        [sys.executable, "-c", CURRENT_SCHEMA_CHAIN_SCRIPT],
        cwd=repository,
        env=environment,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )

    assert completed.returncode == 0, (
        f"stdout:\n{completed.stdout}\n"
        f"stderr:\n{completed.stderr}"
    )


def test_index_migration_preserves_legacy_media_server_semantics(
        monkeypatch,
) -> None:
    """旧字段存在时应保持 2.2.4 的索引替换与回滚语义。"""
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    media_server = sa.Table(
        "mediaserveritem",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tmdbid", sa.Integer()),
        sa.Column("item_type", sa.String()),
    )
    sa.Index("ix_mediaserveritem_id", media_server.c.id)
    sa.Index("ix_mediaserveritem_tmdbid", media_server.c.tmdbid)

    with engine.begin() as connection:
        metadata.create_all(connection)
        migration = _bind_migration(monkeypatch, connection)

        migration.upgrade()
        migration.upgrade()

        upgraded = _index_signatures(connection, "mediaserveritem")
        assert upgraded.get("ix_mediaserveritem_tmdbid_item_type") == (
            ("tmdbid", "item_type"), False,
        )
        assert "ix_mediaserveritem_tmdbid" not in upgraded
        assert "ix_mediaserveritem_id" not in upgraded

        migration.downgrade()

        downgraded = _index_signatures(connection, "mediaserveritem")
        assert "ix_mediaserveritem_tmdbid_item_type" not in downgraded
        assert downgraded.get("ix_mediaserveritem_tmdbid") == (
            ("tmdbid",), False,
        )
        assert downgraded.get("ix_mediaserveritem_id") == (("id",), False)


def test_index_migration_skips_only_indexes_with_missing_columns(
        monkeypatch,
) -> None:
    """当前 schema 应跳过旧字段索引，同时继续处理其他适用索引。"""
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    media_server = sa.Table(
        "mediaserveritem",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("media_source", sa.String()),
        sa.Column("media_id", sa.String()),
        sa.Column("item_type", sa.String()),
    )
    sa.Index(
        "ix_mediaserveritem_media_identity_type",
        media_server.c.media_source,
        media_server.c.media_id,
        media_server.c.item_type,
    )
    message = sa.Table(
        "message",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("reg_time", sa.DateTime()),
    )
    sa.Index("ix_message_reg_time", message.c.reg_time)

    with engine.begin() as connection:
        metadata.create_all(connection)
        migration = _bind_migration(monkeypatch, connection)

        migration.upgrade()

        media_indexes = _index_signatures(connection, "mediaserveritem")
        message_indexes = _index_signatures(connection, "message")
        assert "ix_mediaserveritem_tmdbid_item_type" not in media_indexes
        assert media_indexes.get("ix_mediaserveritem_media_identity_type") == (
            ("media_source", "media_id", "item_type"), False,
        )
        assert "ix_message_reg_time" not in message_indexes
        assert message_indexes.get("ix_message_reg_time_id") == (
            ("reg_time", "id"), False,
        )

        migration.downgrade()

        media_indexes = _index_signatures(connection, "mediaserveritem")
        message_indexes = _index_signatures(connection, "message")
        assert "ix_mediaserveritem_tmdbid" not in media_indexes
        assert media_indexes.get("ix_mediaserveritem_media_identity_type") == (
            ("media_source", "media_id", "item_type"), False,
        )
        assert message_indexes.get("ix_message_reg_time") == (
            ("reg_time",), False,
        )
        assert "ix_message_reg_time_id" not in message_indexes


def test_current_schema_reaches_current_alembic_head(tmp_path: Path) -> None:
    """真实 fresh 启动链应到动态解析的唯一 head，且重复升级保持幂等。"""
    repository = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment.update({
        "CONFIG_DIR": str(tmp_path),
        "DB_TYPE": "sqlite",
        "SUPERUSER": "migration-test-admin",
        "SUPERUSER_PASSWORD": "MigrationTestPassword123",
    })
    _run_current_schema_chain(repository, environment)


def test_current_schema_reaches_current_alembic_head_on_postgresql(
        tmp_path: Path,
) -> None:
    """PostgreSQL fresh schema 应到唯一 head，不能被吞异常伪装成成功。"""
    prefix = "MOVIEPILOT_TEST_POSTGRESQL_"
    host = os.getenv(f"{prefix}HOST")
    database = os.getenv(f"{prefix}DATABASE")
    username = os.getenv(f"{prefix}USERNAME")
    if not host or not database or not username:
        pytest.skip("未配置隔离 PostgreSQL migration 测试库")

    port = os.getenv(f"{prefix}PORT", "5432")
    password = os.getenv(f"{prefix}PASSWORD", "")
    schema = f"p1_db1_{uuid.uuid4().hex}"
    with psycopg2.connect(
        host=host,
        port=port,
        dbname=database,
        user=username,
        password=password,
    ) as connection:
        connection.autocommit = True
        with connection.cursor() as cursor:
            cursor.execute(
                sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema))
            )

    repository = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment.update({
        "CONFIG_DIR": str(tmp_path),
        "DB_TYPE": "postgresql",
        "DB_POSTGRESQL_HOST": host,
        "DB_POSTGRESQL_PORT": port,
        "DB_POSTGRESQL_DATABASE": database,
        "DB_POSTGRESQL_USERNAME": username,
        "DB_POSTGRESQL_PASSWORD": password,
        "PGOPTIONS": f"-c search_path={schema}",
        "SUPERUSER": "migration-test-admin",
        "SUPERUSER_PASSWORD": "MigrationTestPassword123",
    })
    try:
        _run_current_schema_chain(repository, environment)
    finally:
        with psycopg2.connect(
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
