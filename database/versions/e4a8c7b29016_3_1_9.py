"""
3.1.9 为通知历史增加独立媒体海报。

版本标识：e4a8c7b29016
前置版本：da73b6f20418
创建时间：2026-10-24
"""

from alembic import op
import sqlalchemy as sa


revision = "e4a8c7b29016"
down_revision = "da73b6f20418"
branch_labels = None
depends_on = None


def _has_poster() -> bool:
    """
    检查通知表是否已经由新 ORM 或迁移创建海报字段。
    """
    return any(
        column["name"] == "poster"
        for column in sa.inspect(op.get_bind()).get_columns("message")
    )


def upgrade() -> None:
    """
    增加可空海报，不将历史渠道图片误认为媒体海报。
    """
    if not _has_poster():
        op.add_column("message", sa.Column("poster", sa.String(), nullable=True))


def downgrade() -> None:
    """
    移除独立海报字段，保留原有图片与通知内容。
    """
    if _has_poster():
        op.drop_column("message", "poster")
