"""2.2.14
统一媒体身份字段并补齐 Bangumi/AniList ID

Revision ID: f7b2d5c9a301
Revises: e6a1c4b8d2f0
Create Date: 2026-07-21
"""

from typing import Iterable

from alembic import op
import sqlalchemy as sa


revision = "f7b2d5c9a301"
down_revision = "e6a1c4b8d2f0"
branch_labels = None
depends_on = None


def _has_column(
        inspector: sa.Inspector,
        table_name: str,
        column_name: str,
) -> bool:
    """检查数据表是否已存在指定字段。"""
    if table_name not in inspector.get_table_names():
        return False
    return any(
        column["name"] == column_name
        for column in inspector.get_columns(table_name)
    )


def _has_index(
        inspector: sa.Inspector,
        table_name: str,
        index_name: str,
) -> bool:
    """检查数据表是否已存在指定索引。"""
    if table_name not in inspector.get_table_names():
        return False
    return any(
        index["name"] == index_name
        for index in inspector.get_indexes(table_name)
    )


def _add_columns(table_name: str, columns: Iterable[sa.Column]) -> None:
    """为指定表补充尚不存在的字段。"""
    for column in columns:
        inspector = sa.inspect(op.get_bind())
        if not _has_column(inspector, table_name, column.name):
            op.add_column(table_name, column)


def _create_index(table_name: str, index_name: str, columns: list[str]) -> None:
    """为指定表创建尚不存在的索引。"""
    inspector = sa.inspect(op.get_bind())
    if not _has_index(inspector, table_name, index_name):
        op.create_index(index_name, table_name, columns)


def _backfill_media_identity(table_name: str, has_mediaid: bool = False) -> None:
    """使用兼容 ID 幂等回填统一媒体身份。"""
    columns = [
        sa.column("tmdbid", sa.Integer()),
        sa.column("doubanid", sa.String()),
        sa.column("bangumiid", sa.Integer()),
        sa.column("anilistid", sa.Integer()),
        sa.column("media_source", sa.String()),
        sa.column("media_id", sa.String()),
    ]
    if has_mediaid:
        columns.append(sa.column("mediaid", sa.String()))
    table = sa.table(table_name, *columns)
    connection = op.get_bind()

    if has_mediaid:
        for prefix, source in (
                ("tmdb", "themoviedb"),
                ("themoviedb", "themoviedb"),
                ("douban", "douban"),
                ("bangumi", "bangumi"),
                ("anilist", "anilist"),
        ):
            connection.execute(
                table.update()
                .where(table.c.media_id.is_(None))
                .where(table.c.mediaid.like(f"{prefix}:%"))
                .values(
                    media_source=source,
                    media_id=sa.func.substr(table.c.mediaid, len(prefix) + 2),
                )
            )

    for source, field in (
            ("themoviedb", "tmdbid"),
            ("douban", "doubanid"),
            ("bangumi", "bangumiid"),
            ("anilist", "anilistid"),
    ):
        identity_column = table.c[field]
        connection.execute(
            table.update()
            .where(table.c.media_id.is_(None))
            .where(identity_column.is_not(None))
            .values(
                media_source=source,
                media_id=sa.cast(identity_column, sa.String()),
            )
        )


def upgrade() -> None:
    """升级媒体身份字段并迁移存量数据。"""
    _add_columns("subscribe", (
        sa.Column("anilistid", sa.Integer(), nullable=True),
        sa.Column("media_source", sa.String(), nullable=True),
        sa.Column("media_id", sa.String(), nullable=True),
    ))
    _add_columns("subscribehistory", (
        sa.Column("anilistid", sa.Integer(), nullable=True),
        sa.Column("media_source", sa.String(), nullable=True),
        sa.Column("media_id", sa.String(), nullable=True),
    ))
    _add_columns("downloadhistory", (
        sa.Column("bangumiid", sa.Integer(), nullable=True),
        sa.Column("anilistid", sa.Integer(), nullable=True),
        sa.Column("media_source", sa.String(), nullable=True),
        sa.Column("media_id", sa.String(), nullable=True),
    ))
    _add_columns("transferhistory", (
        sa.Column("bangumiid", sa.Integer(), nullable=True),
        sa.Column("anilistid", sa.Integer(), nullable=True),
    ))
    _add_columns("downloadfailure", (
        sa.Column("bangumiid", sa.Integer(), nullable=True),
        sa.Column("anilistid", sa.Integer(), nullable=True),
        sa.Column("media_source", sa.String(), nullable=True),
        sa.Column("media_id", sa.String(), nullable=True),
    ))

    for table_name, fields in {
        "subscribe": ("anilistid", "media_source", "media_id"),
        "subscribehistory": ("anilistid", "media_source", "media_id"),
        "downloadhistory": ("bangumiid", "anilistid", "media_source", "media_id"),
        "transferhistory": ("bangumiid", "anilistid"),
    }.items():
        for field in fields:
            _create_index(table_name, f"ix_{table_name}_{field}", [field])

    for table_name in ("subscribe", "subscribehistory", "downloadhistory", "transferhistory"):
        _create_index(
            table_name,
            f"ix_{table_name}_media_identity",
            ["media_source", "media_id"],
        )
    _create_index(
        "downloadfailure",
        "ix_downloadfailure_media_identity_site",
        ["type", "media_source", "media_id", "site"],
    )

    _backfill_media_identity("subscribe", has_mediaid=True)
    _backfill_media_identity("subscribehistory", has_mediaid=True)
    _backfill_media_identity("downloadhistory")
    _backfill_media_identity("transferhistory")
    _backfill_media_identity("downloadfailure")


def downgrade() -> None:
    """回滚统一媒体身份及 Bangumi/AniList 字段。"""
    for table_name, index_names in {
        "subscribe": (
            "ix_subscribe_media_identity", "ix_subscribe_media_id",
            "ix_subscribe_media_source", "ix_subscribe_anilistid",
        ),
        "subscribehistory": (
            "ix_subscribehistory_media_identity", "ix_subscribehistory_media_id",
            "ix_subscribehistory_media_source", "ix_subscribehistory_anilistid",
        ),
        "downloadhistory": (
            "ix_downloadhistory_media_identity", "ix_downloadhistory_media_id",
            "ix_downloadhistory_media_source", "ix_downloadhistory_anilistid",
            "ix_downloadhistory_bangumiid",
        ),
        "transferhistory": (
            "ix_transferhistory_media_identity", "ix_transferhistory_anilistid",
            "ix_transferhistory_bangumiid",
        ),
        "downloadfailure": ("ix_downloadfailure_media_identity_site",),
    }.items():
        for index_name in index_names:
            inspector = sa.inspect(op.get_bind())
            if _has_index(inspector, table_name, index_name):
                op.drop_index(index_name, table_name=table_name)

    for table_name, fields in {
        "subscribe": ("media_id", "media_source", "anilistid"),
        "subscribehistory": ("media_id", "media_source", "anilistid"),
        "downloadhistory": ("media_id", "media_source", "anilistid", "bangumiid"),
        "transferhistory": ("anilistid", "bangumiid"),
        "downloadfailure": ("media_id", "media_source", "anilistid", "bangumiid"),
    }.items():
        for field in fields:
            inspector = sa.inspect(op.get_bind())
            if _has_column(inspector, table_name, field):
                op.drop_column(table_name, field)
