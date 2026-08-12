"""3.0.0
统一通用媒体表的来源与原生 ID

Revision ID: 8a4c7e1d2f90
Revises: 6f9a1c2d3e4b
Create Date: 2026-08-12
"""

from collections.abc import Iterable

from alembic import op
import sqlalchemy as sa


revision = "8a4c7e1d2f90"
down_revision = "6f9a1c2d3e4b"
branch_labels = None
depends_on = None


LEGACY_COLUMNS = {
    "subscribe": (
        "tmdbid", "imdbid", "tvdbid", "doubanid", "bangumiid",
        "anilistid", "mediaid",
    ),
    "subscribehistory": (
        "tmdbid", "imdbid", "tvdbid", "doubanid", "bangumiid",
        "anilistid", "mediaid",
    ),
    "downloadhistory": (
        "tmdbid", "imdbid", "tvdbid", "doubanid", "bangumiid",
        "anilistid",
    ),
    "transferhistory": (
        "tmdbid", "imdbid", "tvdbid", "doubanid", "bangumiid",
        "anilistid",
    ),
    "downloadfailure": ("tmdbid", "doubanid", "bangumiid", "anilistid"),
    "mediaserveritem": ("tmdbid", "imdbid", "tvdbid"),
}

SOURCE_COLUMNS = (
    ("themoviedb", "tmdbid"),
    ("douban", "doubanid"),
    ("bangumi", "bangumiid"),
    ("anilist", "anilistid"),
    ("imdb", "imdbid"),
    ("tvdb", "tvdbid"),
)

SOURCE_ALIASES = {
    "tmdb": "themoviedb",
    "themoviedb": "themoviedb",
    "douban": "douban",
    "bangumi": "bangumi",
    "anilist": "anilist",
    "imdb": "imdb",
    "tvdb": "tvdb",
    "musicbrainz": "musicbrainz",
    "theaudiodb": "theaudiodb",
    "audio_db": "theaudiodb",
    "doubanmusic": "doubanmusic",
    "douban_music": "doubanmusic",
    "bilibili": "bilibili",
    "mangguodiscover": "mangguodiscover",
    "mango_tv": "mangguodiscover",
    "migu": "migu",
    "migu_video": "migu",
    "tencentvideodiscover": "tencentvideodiscover",
    "tencent_video": "tencentvideodiscover",
}
MEDIA_SOURCE_VALUES = frozenset(SOURCE_ALIASES.values())
MEDIA_SOURCE_SQL_VALUES = ", ".join(
    f"'{source}'" for source in sorted(MEDIA_SOURCE_VALUES)
)
MEDIA_IDENTITY_CHECK_SQL = (
    "(media_source IS NULL AND media_id IS NULL) OR "
    "(media_source IS NOT NULL AND "
    f"media_source IN ({MEDIA_SOURCE_SQL_VALUES}) AND "
    "media_id IS NOT NULL AND trim(media_id) <> '' AND trim(media_id) <> '0')"
)


def _inspector() -> sa.Inspector:
    """返回使用当前迁移连接的数据库检查器。"""
    return sa.inspect(op.get_bind())


def _has_table(table_name: str) -> bool:
    """检查数据表是否存在。"""
    return table_name in _inspector().get_table_names()


def _column_names(table_name: str) -> set[str]:
    """读取数据表当前全部字段名。"""
    if not _has_table(table_name):
        return set()
    return {column["name"] for column in _inspector().get_columns(table_name)}


def _ensure_identity_columns(table_name: str) -> None:
    """为旧库补齐规范媒体身份字段。"""
    columns = _column_names(table_name)
    if "media_source" not in columns:
        op.add_column(table_name, sa.Column("media_source", sa.String(), nullable=True))
    if "media_id" not in columns:
        op.add_column(table_name, sa.Column("media_id", sa.String(), nullable=True))


def _identity_missing(table: sa.TableClause):
    """返回任一规范身份字段为空的 SQL 条件。"""
    return sa.or_(
        table.c.media_source.is_(None),
        table.c.media_source == "",
        table.c.media_id.is_(None),
        table.c.media_id == "",
    )


def _normalize_existing_sources(table_name: str) -> None:
    """把旧版本允许的来源别名规范化为当前枚举值。"""
    table = sa.table(
        table_name,
        sa.column("media_source", sa.String()),
    )
    connection = op.get_bind()
    for alias, source in SOURCE_ALIASES.items():
        connection.execute(
            table.update()
            .where(sa.func.lower(sa.func.trim(table.c.media_source)) == alias)
            .values(media_source=source)
        )
    connection.execute(
        table.update()
        .where(table.c.media_source.is_not(None))
        .values(media_source=sa.func.trim(table.c.media_source))
    )


def _clear_invalid_or_partial_identity(table_name: str) -> None:
    """清空无效或仅有一半的身份，允许后续从旧字段重新回填。"""
    table = sa.table(
        table_name,
        sa.column("media_source", sa.String()),
        sa.column("media_id", sa.String()),
    )
    invalid_identity = sa.or_(
        table.c.media_source.is_(None),
        sa.func.trim(table.c.media_source) == "",
        sa.func.lower(sa.func.trim(table.c.media_source)).not_in(
            MEDIA_SOURCE_VALUES
        ),
        table.c.media_id.is_(None),
        sa.func.trim(table.c.media_id) == "",
        sa.func.trim(table.c.media_id) == "0",
    )
    op.get_bind().execute(
        table.update()
        .where(invalid_identity)
        .values(media_source=None, media_id=None)
    )
    op.get_bind().execute(
        table.update()
        .where(table.c.media_id.is_not(None))
        .values(media_id=sa.func.trim(table.c.media_id))
    )


def _backfill_prefixed_media_id(table_name: str, columns: set[str]) -> None:
    """从旧的 ``prefix:id`` 组合字段回填规范身份。"""
    if "mediaid" not in columns:
        return
    table = sa.table(
        table_name,
        sa.column("mediaid", sa.String()),
        sa.column("media_source", sa.String()),
        sa.column("media_id", sa.String()),
    )
    for prefix, source in SOURCE_ALIASES.items():
        op.get_bind().execute(
            table.update()
            .where(_identity_missing(table))
            .where(
                sa.func.lower(
                    sa.func.substr(
                        sa.func.trim(table.c.mediaid), 1, len(prefix) + 1
                    )
                ) == f"{prefix}:"
            )
            .where(
                sa.func.trim(
                    sa.func.substr(table.c.mediaid, len(prefix) + 2)
                ) != ""
            )
            .where(
                sa.func.trim(
                    sa.func.substr(table.c.mediaid, len(prefix) + 2)
                ) != "0"
            )
            .values(
                media_source=source,
                media_id=sa.func.trim(
                    sa.func.substr(table.c.mediaid, len(prefix) + 2)
                ),
            )
        )


def _backfill_source_columns(table_name: str, columns: set[str]) -> None:
    """按确定优先级从旧的来源专用字段回填规范身份。"""
    table_columns = [
        sa.column("media_source", sa.String()),
        sa.column("media_id", sa.String()),
    ] + [
        sa.column(field, sa.String())
        for _, field in SOURCE_COLUMNS
        if field in columns
    ]
    table = sa.table(table_name, *table_columns)
    for source, field in SOURCE_COLUMNS:
        if field not in columns:
            continue
        identity_column = table.c[field]
        op.get_bind().execute(
            table.update()
            .where(_identity_missing(table))
            .where(identity_column.is_not(None))
            .where(sa.func.trim(sa.cast(identity_column, sa.String())) != "")
            .where(sa.func.trim(sa.cast(identity_column, sa.String())) != "0")
            .values(
                media_source=source,
                media_id=sa.func.trim(sa.cast(identity_column, sa.String())),
            )
        )


def _drop_legacy_indexes(table_name: str, columns: Iterable[str]) -> None:
    """删除引用待移除字段的普通索引或唯一约束。"""
    legacy_columns = set(columns)
    inspector = _inspector()
    for index in inspector.get_indexes(table_name):
        if legacy_columns.intersection(index.get("column_names") or []):
            op.drop_index(index["name"], table_name=table_name)
    inspector = _inspector()
    for constraint in inspector.get_unique_constraints(table_name):
        if legacy_columns.intersection(constraint.get("column_names") or []):
            name = constraint.get("name")
            if name:
                op.drop_constraint(name, table_name, type_="unique")


def _drop_legacy_columns(table_name: str, columns: Iterable[str]) -> None:
    """以批处理方式移除旧媒体身份字段，兼容 SQLite。"""
    existing = _column_names(table_name)
    targets = [column for column in columns if column in existing]
    if not targets:
        return
    _drop_legacy_indexes(table_name, targets)
    with op.batch_alter_table(table_name) as batch_op:
        for column in targets:
            batch_op.drop_column(column)


def _ensure_identity_indexes() -> None:
    """为规范身份字段建立查询索引。"""
    for table_name in LEGACY_COLUMNS:
        if not _has_table(table_name):
            continue
        existing = {index["name"] for index in _inspector().get_indexes(table_name)}
        source_index = f"ix_{table_name}_media_source"
        id_index = f"ix_{table_name}_media_id"
        identity_index = f"ix_{table_name}_media_identity"
        if source_index not in existing:
            op.create_index(source_index, table_name, ["media_source"])
        if id_index not in existing:
            op.create_index(id_index, table_name, ["media_id"])
        existing = {index["name"] for index in _inspector().get_indexes(table_name)}
        if table_name != "downloadfailure" and identity_index not in existing:
            op.create_index(identity_index, table_name, ["media_source", "media_id"])
    if _has_table("downloadfailure"):
        existing = {
            index["name"] for index in _inspector().get_indexes("downloadfailure")
        }
        identity_site = "ix_downloadfailure_media_identity_site"
        if identity_site not in existing:
            op.create_index(
                identity_site,
                "downloadfailure",
                ["type", "media_source", "media_id", "site"],
            )
    if _has_table("mediaserveritem"):
        existing = {
            index["name"] for index in _inspector().get_indexes("mediaserveritem")
        }
        identity_type = "ix_mediaserveritem_media_identity_type"
        if identity_type not in existing:
            op.create_index(
                identity_type,
                "mediaserveritem",
                ["media_source", "media_id", "item_type"],
            )


def _ensure_identity_constraints() -> None:
    """为六张通用媒体表建立来源枚举与身份成对数据库约束。"""
    for table_name in LEGACY_COLUMNS:
        if not _has_table(table_name):
            continue
        constraint_name = f"ck_{table_name}_media_identity"
        existing = {
            constraint.get("name")
            for constraint in _inspector().get_check_constraints(table_name)
        }
        if constraint_name in existing:
            continue
        with op.batch_alter_table(table_name) as batch_op:
            batch_op.create_check_constraint(
                constraint_name,
                MEDIA_IDENTITY_CHECK_SQL,
            )


def _drop_identity_constraints() -> None:
    """降级时移除本次迁移新增的媒体身份数据库约束。"""
    for table_name in LEGACY_COLUMNS:
        if not _has_table(table_name):
            continue
        constraint_name = f"ck_{table_name}_media_identity"
        existing = {
            constraint.get("name")
            for constraint in _inspector().get_check_constraints(table_name)
        }
        if constraint_name not in existing:
            continue
        with op.batch_alter_table(table_name) as batch_op:
            batch_op.drop_constraint(constraint_name, type_="check")


def upgrade() -> None:
    """回填规范媒体身份，并删除通用表中的全部来源专用 ID 字段。"""
    for table_name, legacy_columns in LEGACY_COLUMNS.items():
        if not _has_table(table_name):
            continue
        _ensure_identity_columns(table_name)
        _normalize_existing_sources(table_name)
        _clear_invalid_or_partial_identity(table_name)
        columns = _column_names(table_name)
        _backfill_prefixed_media_id(table_name, columns)
        _backfill_source_columns(table_name, columns)
        _drop_legacy_columns(table_name, legacy_columns)
    _ensure_identity_indexes()
    _ensure_identity_constraints()


def _restore_legacy_columns(table_name: str, columns: Iterable[str]) -> None:
    """降级时恢复旧字段，并从当前主身份回填能够确定的来源字段。"""
    if not _has_table(table_name):
        return
    existing = _column_names(table_name)
    with op.batch_alter_table(table_name) as batch_op:
        for column in columns:
            if column in existing:
                continue
            column_type = sa.Integer() if column in {
                "tmdbid", "tvdbid", "bangumiid", "anilistid"
            } else sa.String()
            batch_op.add_column(sa.Column(column, column_type, nullable=True))
    columns = _column_names(table_name)
    table = sa.table(
        table_name,
        sa.column("media_source", sa.String()),
        sa.column("media_id", sa.String()),
        *[sa.column(column, sa.String()) for column in columns if column in LEGACY_COLUMNS[table_name]],
    )
    for source, field in SOURCE_COLUMNS:
        if field not in columns:
            continue
        value = table.c.media_id
        if field in {"tmdbid", "tvdbid", "bangumiid", "anilistid"}:
            value = sa.cast(table.c.media_id, sa.Integer())
        op.get_bind().execute(
            table.update()
            .where(table.c.media_source == source)
            .where(table.c.media_id.is_not(None))
            .values({field: value})
        )


def downgrade() -> None:
    """恢复旧列；已被规范身份舍弃的辅助来源 ID 无法无损恢复。"""
    _drop_identity_constraints()
    for table_name, legacy_columns in LEGACY_COLUMNS.items():
        _restore_legacy_columns(table_name, legacy_columns)
    if _has_table("mediaserveritem"):
        existing_indexes = {
            index["name"] for index in _inspector().get_indexes("mediaserveritem")
        }
        for index_name in (
            "ix_mediaserveritem_media_identity_type",
            "ix_mediaserveritem_media_identity",
            "ix_mediaserveritem_media_source",
            "ix_mediaserveritem_media_id",
        ):
            if index_name in existing_indexes:
                op.drop_index(index_name, table_name="mediaserveritem")
        with op.batch_alter_table("mediaserveritem") as batch_op:
            batch_op.drop_column("media_id")
            batch_op.drop_column("media_source")
