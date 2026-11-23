"""
新增 downloadpending 表，保存下载器已受理但尚未确认的任务核对状态。
"""

import sqlalchemy as sa
from alembic import op

revision = "c4e8f1a29b03"
down_revision = "d8a4c6e29170"
branch_labels = None
depends_on = None

# 保留迁移时点的媒体身份约束副本，不依赖模型模块后续演进。
_MEDIA_IDENTITY_CHECK_SQL = (
    "(media_source IS NULL AND media_id IS NULL) OR "
    "(media_source IS NOT NULL AND "
    "trim(media_source) <> '' AND media_source = lower(trim(media_source)) AND "
    "length(media_source) <= 64 AND media_source NOT LIKE '%:%' AND "
    "media_source NOT LIKE '% %' AND "
    "media_id IS NOT NULL AND trim(media_id) <> '' AND trim(media_id) <> '0')"
)


def upgrade() -> None:
    """
    幂等创建 downloadpending 表及其约束与索引。
    """
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "downloadpending" not in inspector.get_table_names():
        op.create_table(
            "downloadpending",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("pending_key", sa.String(), nullable=False),
            sa.Column("target_key", sa.String(), nullable=False),
            sa.Column("type", sa.String(), nullable=True),
            sa.Column("title", sa.String(), nullable=True),
            sa.Column("year", sa.String(), nullable=True),
            sa.Column("media_source", sa.String(), nullable=True),
            sa.Column("media_id", sa.String(), nullable=True),
            sa.Column("season", sa.Integer(), nullable=True),
            sa.Column("episodes", sa.JSON(), nullable=True),
            sa.Column("downloader", sa.String(), nullable=True),
            sa.Column("hashes", sa.JSON(), nullable=True),
            sa.Column("torrent_name", sa.String(), nullable=True),
            sa.Column("source", sa.String(), nullable=True),
            sa.Column("subscription_id", sa.Integer(), nullable=True),
            sa.Column("payload", sa.JSON(), nullable=True),
            sa.Column("submitted_at", sa.String(), nullable=False),
            sa.Column("last_checked_at", sa.String(), nullable=True),
            sa.Column("expires_at", sa.String(), nullable=False),
            sa.CheckConstraint(
                _MEDIA_IDENTITY_CHECK_SQL,
                name="ck_downloadpending_media_identity",
            ),
        )
    existing_indexes = {
        index["name"] for index in sa.inspect(bind).get_indexes("downloadpending")
    }
    if "ux_downloadpending_pending_key" not in existing_indexes:
        op.create_index(
            "ux_downloadpending_pending_key",
            "downloadpending",
            ["pending_key"],
            unique=True,
        )
    if "ix_downloadpending_target_key" not in existing_indexes:
        op.create_index(
            "ix_downloadpending_target_key",
            "downloadpending",
            ["target_key"],
            unique=False,
        )
    if "ix_downloadpending_subscription_id" not in existing_indexes:
        op.create_index(
            "ix_downloadpending_subscription_id",
            "downloadpending",
            ["subscription_id"],
            unique=False,
        )
    if "ix_downloadpending_expires_at" not in existing_indexes:
        op.create_index(
            "ix_downloadpending_expires_at",
            "downloadpending",
            ["expires_at"],
            unique=False,
        )


def downgrade() -> None:
    """
    移除 downloadpending 表及其索引。
    """
    bind = op.get_bind()
    if "downloadpending" in sa.inspect(bind).get_table_names():
        op.drop_table("downloadpending")
