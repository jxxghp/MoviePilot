"""2.2.16
为音乐订阅保存实体类型和专辑总曲目数

Revision ID: c9d4e7f1a2b3
Revises: a8c4e2f6b1d9
Create Date: 2026-08-09
"""

from alembic import op
import sqlalchemy as sa


revision = "c9d4e7f1a2b3"
down_revision = "a8c4e2f6b1d9"
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


def _add_music_columns(table_name: str) -> None:
    """为订阅或订阅历史表幂等补充音乐专辑字段。"""
    if not _has_column(table_name, "music_type"):
        op.add_column(
            table_name,
            sa.Column("music_type", sa.String(), nullable=True),
        )
    if not _has_column(table_name, "total_tracks"):
        op.add_column(
            table_name,
            sa.Column("total_tracks", sa.Integer(), nullable=True),
        )


def upgrade() -> None:
    """为当前订阅及完成历史补充音乐专辑字段。"""
    _add_music_columns("subscribe")
    _add_music_columns("subscribehistory")


def downgrade() -> None:
    """移除音乐订阅专辑字段。"""
    for table_name in ("subscribehistory", "subscribe"):
        if _has_column(table_name, "total_tracks"):
            op.drop_column(table_name, "total_tracks")
        if _has_column(table_name, "music_type"):
            op.drop_column(table_name, "music_type")
