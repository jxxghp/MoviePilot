"""2.2.7
为订阅洗版增加分集和仅全集模式

Revision ID: 09c91570f4a2
Revises: 9caa49cb3e10
Create Date: 2026-05-13
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "09c91570f4a2"
down_revision = "9caa49cb3e10"
branch_labels = None
depends_on = None


def _has_column(inspector: sa.Inspector, table_name: str, column_name: str) -> bool:
    """判断指定表是否已经存在目标列，支持重复执行迁移时安全跳过。"""
    if table_name not in inspector.get_table_names():
        return False
    return any(column["name"] == column_name for column in inspector.get_columns(table_name))


def upgrade() -> None:
    """新增订阅洗版模式字段，历史订阅保持空值并按系统默认解释。"""
    inspector = sa.inspect(op.get_bind())
    if _has_column(inspector, "subscribe", "best_version_mode") is False:
        op.add_column("subscribe", sa.Column("best_version_mode", sa.String(), nullable=True))

    inspector = sa.inspect(op.get_bind())
    if _has_column(inspector, "subscribehistory", "best_version_mode") is False:
        op.add_column("subscribehistory", sa.Column("best_version_mode", sa.String(), nullable=True))


def downgrade() -> None:
    """回滚订阅洗版模式字段。"""
    inspector = sa.inspect(op.get_bind())
    if _has_column(inspector, "subscribehistory", "best_version_mode"):
        op.drop_column("subscribehistory", "best_version_mode")

    inspector = sa.inspect(op.get_bind())
    if _has_column(inspector, "subscribe", "best_version_mode"):
        op.drop_column("subscribe", "best_version_mode")
