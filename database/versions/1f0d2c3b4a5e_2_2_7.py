"""
2.2.7 为电视剧洗版增加全集整包开关

版本标识： 1f0d2c3b4a5e
前置版本： 9caa49cb3e10
创建时间： 2026-05-13
"""

from alembic import op
import sqlalchemy as sa

# Alembic 使用以下标识连接迁移版本链。
revision = "1f0d2c3b4a5e"
down_revision = "9caa49cb3e10"
branch_labels = None
depends_on = None


def _has_column(inspector: sa.Inspector, table_name: str, column_name: str) -> bool:
    """
    确认表存在后检查指定字段，缺表时返回 False。
    """
    if table_name not in inspector.get_table_names():
        return False
    return any(column["name"] == column_name for column in inspector.get_columns(table_name))


def upgrade() -> None:
    """
    为订阅及完成历史补齐整季整包洗版开关，旧记录默认关闭。
    """
    inspector = sa.inspect(op.get_bind())
    if _has_column(inspector, "subscribe", "best_version_full") is False:
        op.add_column("subscribe", sa.Column("best_version_full", sa.Integer(), nullable=True, server_default="0"))

    inspector = sa.inspect(op.get_bind())
    if _has_column(inspector, "subscribehistory", "best_version_full") is False:
        op.add_column("subscribehistory", sa.Column("best_version_full", sa.Integer(), nullable=True, server_default="0"))


def downgrade() -> None:
    """
    移除订阅及完成历史中的整季整包洗版开关。
    """
    inspector = sa.inspect(op.get_bind())
    if _has_column(inspector, "subscribehistory", "best_version_full"):
        op.drop_column("subscribehistory", "best_version_full")

    inspector = sa.inspect(op.get_bind())
    if _has_column(inspector, "subscribe", "best_version_full"):
        op.drop_column("subscribe", "best_version_full")
