"""3.0.30 增加逐订阅搜索周期和持久搜索时间。"""

import sqlalchemy as sa
from alembic import op

revision = "f8a2c6e9b1d4"
down_revision = "e7f3a9c1d5b2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """兼容首次建表和已有数据库，旧订阅默认跟随系统周期。"""
    inspector = sa.inspect(op.get_bind())
    for table in ("subscribe", "subscribehistory"):
        columns = {column["name"] for column in inspector.get_columns(table)}
        if "search_interval" not in columns:
            op.add_column(table, sa.Column("search_interval", sa.Integer(), nullable=True))
        if table == "subscribe" and "last_search" not in columns:
            op.add_column(table, sa.Column("last_search", sa.String(), nullable=True))


def downgrade() -> None:
    """移除新增周期字段，保留原有订阅和历史数据。"""
    op.drop_column("subscribe", "last_search")
    for table in ("subscribe", "subscribehistory"):
        op.drop_column(table, "search_interval")
