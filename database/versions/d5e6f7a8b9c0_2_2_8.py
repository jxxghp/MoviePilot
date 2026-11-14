"""
2.2.8 修复 episode_priority 列类型：PostgreSQL 下若列为 INTEGER 则重建为 JSON

版本标识： d5e6f7a8b9c0
前置版本： 1f0d2c3b4a5e
创建时间： 2026-06-04
"""

from alembic import op
import sqlalchemy as sa

revision = "d5e6f7a8b9c0"
down_revision = "1f0d2c3b4a5e"
branch_labels = None
depends_on = None


def _has_column(inspector: sa.Inspector, table_name: str, column_name: str) -> bool:
    """
    确认表存在后检查指定字段，缺表时返回 False。
    """
    if table_name not in inspector.get_table_names():
        return False
    return any(column["name"] == column_name for column in inspector.get_columns(table_name))


def _fix_episode_priority_type(bind, table_name: str) -> None:
    """
    在 PostgreSQL 上补齐逐集优先级列，已有列类型错误时删除并重建为 JSON。

    已有 JSON 或 JSONB 列保持不变；重建会丢弃旧列中的值。
    """
    if bind.dialect.name != "postgresql":
        return
    inspector = sa.inspect(bind)
    if not _has_column(inspector, table_name, "episode_priority"):
        op.add_column(table_name, sa.Column("episode_priority", sa.JSON(), nullable=True))
        return
    for col in inspector.get_columns(table_name):
        if col["name"] != "episode_priority":
            continue
        type_name = type(col["type"]).__name__.upper()
        if type_name in ("JSON", "JSONB"):
            return
        op.drop_column(table_name, "episode_priority")
        op.add_column(table_name, sa.Column("episode_priority", sa.JSON(), nullable=True))
        return


def upgrade() -> None:
    """
    仅在 PostgreSQL 上补齐或重建逐集优先级 JSON 列。
    """
    bind = op.get_bind()
    _fix_episode_priority_type(bind, "subscribe")
    _fix_episode_priority_type(bind, "subscribehistory")


def downgrade() -> None:
    """
    本版本不执行回退操作，保留升级后的结构与数据。
    """
    pass
