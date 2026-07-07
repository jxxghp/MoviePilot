"""2.2.11
新增下载失败资源冷却表

Revision ID: b7d4a9c2e6f1
Revises: 8ab72c49d1e3
Create Date: 2026-07-07
"""

from alembic import op
import sqlalchemy as sa

revision = "b7d4a9c2e6f1"
down_revision = "8ab72c49d1e3"
branch_labels = None
depends_on = None


def _has_table(inspector: sa.Inspector, table_name: str) -> bool:
    """检查数据表是否已存在。"""
    return table_name in inspector.get_table_names()


def upgrade() -> None:
    """升级数据库结构。"""
    inspector = sa.inspect(op.get_bind())
    if _has_table(inspector, "downloadfailure"):
        return

    op.create_table(
        "downloadfailure",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("fingerprint", sa.String(), nullable=False),
        sa.Column("type", sa.String(), nullable=True),
        sa.Column("title", sa.String(), nullable=True),
        sa.Column("year", sa.String(), nullable=True),
        sa.Column("tmdbid", sa.Integer(), nullable=True),
        sa.Column("doubanid", sa.String(), nullable=True),
        sa.Column("seasons", sa.String(), nullable=True),
        sa.Column("episodes", sa.String(), nullable=True),
        sa.Column("site", sa.Integer(), nullable=True),
        sa.Column("site_name", sa.String(), nullable=True),
        sa.Column("torrent_id", sa.String(), nullable=True),
        sa.Column("torrent_name", sa.String(), nullable=True),
        sa.Column("torrent_size", sa.Float(), nullable=True),
        sa.Column("downloader", sa.String(), nullable=True),
        sa.Column("source", sa.String(), nullable=True),
        sa.Column("error_message", sa.String(), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=True),
        sa.Column("first_failed_at", sa.String(), nullable=True),
        sa.Column("last_failed_at", sa.String(), nullable=True),
        sa.Column("next_retry_at", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ux_downloadfailure_fingerprint",
        "downloadfailure",
        ["fingerprint"],
        unique=True,
    )
    op.create_index(
        "ix_downloadfailure_next_retry_at",
        "downloadfailure",
        ["next_retry_at"],
    )
    op.create_index(
        "ix_downloadfailure_media_site",
        "downloadfailure",
        ["type", "tmdbid", "doubanid", "site"],
    )


def downgrade() -> None:
    """回滚数据库结构。"""
    inspector = sa.inspect(op.get_bind())
    if not _has_table(inspector, "downloadfailure"):
        return
    op.drop_index("ix_downloadfailure_media_site", table_name="downloadfailure")
    op.drop_index("ix_downloadfailure_next_retry_at", table_name="downloadfailure")
    op.drop_index("ux_downloadfailure_fingerprint", table_name="downloadfailure")
    op.drop_table("downloadfailure")
