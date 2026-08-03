"""2.2.15
为下载历史增加海报字段

Revision ID: a8c4e2f6b1d9
Revises: f7b2d5c9a301
Create Date: 2026-08-03
"""

from alembic import op
import sqlalchemy as sa


revision = "a8c4e2f6b1d9"
down_revision = "f7b2d5c9a301"
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
    """为下载历史增加可空海报字段。"""
    if not _has_column("downloadhistory", "poster"):
        op.add_column(
            "downloadhistory",
            sa.Column("poster", sa.String(), nullable=True),
        )


def downgrade() -> None:
    """移除下载历史海报字段。"""
    if _has_column("downloadhistory", "poster"):
        op.drop_column("downloadhistory", "poster")
