"""2.2.17
为整理历史保存音乐实体类型和专辑总曲目数

Revision ID: d4f6a8c2e1b7
Revises: c9d4e7f1a2b3
Create Date: 2026-08-10
"""

from alembic import op
import sqlalchemy as sa


revision = "d4f6a8c2e1b7"
down_revision = "c9d4e7f1a2b3"
branch_labels = None
depends_on = None


def _has_column(table_name: str, column_name: str) -> bool:
    """检查数据表是否已存在指定字段。"""
    inspector = sa.inspect(op.get_bind())
    if table_name not in inspector.get_table_names():
        return False
    return any(
        column["name"] == column_name
        for column in inspector.get_columns(table_name)
    )


def upgrade() -> None:
    """为整理历史补充音乐重试所需的实体字段。"""
    if not _has_column("transferhistory", "music_type"):
        op.add_column(
            "transferhistory",
            sa.Column("music_type", sa.String(), nullable=True),
        )
    if not _has_column("transferhistory", "total_tracks"):
        op.add_column(
            "transferhistory",
            sa.Column("total_tracks", sa.Integer(), nullable=True),
        )


def downgrade() -> None:
    """移除整理历史中的音乐实体字段。"""
    if _has_column("transferhistory", "total_tracks"):
        op.drop_column("transferhistory", "total_tracks")
    if _has_column("transferhistory", "music_type"):
        op.drop_column("transferhistory", "music_type")
