"""2.2.13
为整理历史增加统一媒体数据源与原生ID

Revision ID: e6a1c4b8d2f0
Revises: c4e8f7a1b2d3
Create Date: 2026-07-21
"""

from alembic import op
import sqlalchemy as sa

revision = "e6a1c4b8d2f0"
down_revision = "c4e8f7a1b2d3"
branch_labels = None
depends_on = None


def _has_column(
    inspector: sa.Inspector,
    table_name: str,
    column_name: str,
) -> bool:
    """
    检查数据表是否已存在指定字段。

    :param inspector: SQLAlchemy结构检查器
    :param table_name: 数据表名称
    :param column_name: 字段名称
    :return: 字段是否存在
    """
    if table_name not in inspector.get_table_names():
        return False
    return any(
        column["name"] == column_name
        for column in inspector.get_columns(table_name)
    )


def upgrade() -> None:
    """升级整理历史数据源字段。"""
    inspector = sa.inspect(op.get_bind())
    if not _has_column(inspector, "transferhistory", "media_source"):
        op.add_column(
            "transferhistory",
            sa.Column("media_source", sa.String(), nullable=True),
        )
        op.create_index(
            "ix_transferhistory_media_source",
            "transferhistory",
            ["media_source"],
        )

    inspector = sa.inspect(op.get_bind())
    if not _has_column(inspector, "transferhistory", "media_id"):
        op.add_column(
            "transferhistory",
            sa.Column("media_id", sa.String(), nullable=True),
        )
        op.create_index(
            "ix_transferhistory_media_id",
            "transferhistory",
            ["media_id"],
        )

    transfer_history = sa.table(
        "transferhistory",
        sa.column("tmdbid", sa.Integer()),
        sa.column("doubanid", sa.String()),
        sa.column("media_source", sa.String()),
        sa.column("media_id", sa.String()),
    )
    connection = op.get_bind()
    connection.execute(
        transfer_history.update()
        .where(transfer_history.c.tmdbid.is_not(None))
        .where(transfer_history.c.media_id.is_(None))
        .values(
            media_source="themoviedb",
            media_id=sa.cast(transfer_history.c.tmdbid, sa.String()),
        )
    )
    connection.execute(
        transfer_history.update()
        .where(transfer_history.c.tmdbid.is_(None))
        .where(transfer_history.c.doubanid.is_not(None))
        .where(transfer_history.c.media_id.is_(None))
        .values(
            media_source="douban",
            media_id=transfer_history.c.doubanid,
        )
    )


def downgrade() -> None:
    """回滚整理历史数据源字段。"""
    inspector = sa.inspect(op.get_bind())
    if _has_column(inspector, "transferhistory", "media_id"):
        op.drop_index("ix_transferhistory_media_id", table_name="transferhistory")
        op.drop_column("transferhistory", "media_id")

    inspector = sa.inspect(op.get_bind())
    if _has_column(inspector, "transferhistory", "media_source"):
        op.drop_index(
            "ix_transferhistory_media_source",
            table_name="transferhistory",
        )
        op.drop_column("transferhistory", "media_source")
