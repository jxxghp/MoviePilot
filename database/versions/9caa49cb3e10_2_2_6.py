"""
2.2.6 为订阅洗版增加按集优先级状态

版本标识： 9caa49cb3e10
前置版本： b8f6e3a1c2d4
创建时间： 2026-05-12
"""

from alembic import op
import sqlalchemy as sa

# Alembic 使用以下标识连接迁移版本链。
revision = "9caa49cb3e10"
down_revision = "b8f6e3a1c2d4"
branch_labels = None
depends_on = None


def _has_column(inspector: sa.Inspector, table_name: str, column_name: str) -> bool:
    """
    确认表存在后检查指定字段，缺表时返回 False。
    """
    if table_name not in inspector.get_table_names():
        return False
    return any(column["name"] == column_name for column in inspector.get_columns(table_name))


def _ensure_json_column(bind, table_name: str, column_name: str) -> None:
    """
    缺失时新增 JSON 列；仅在 PostgreSQL 上重建已有但类型错误的列。

    重建会丢弃旧列的值，保留正确 JSON 或 JSONB 类型时不改写数据。
    """
    inspector = sa.inspect(bind)
    if not _has_column(inspector, table_name, column_name):
        op.add_column(table_name, sa.Column(column_name, sa.JSON(), nullable=True))
        return
    if bind.dialect.name != "postgresql":
        return
    for col in inspector.get_columns(table_name):
        if col["name"] != column_name:
            continue
        type_name = type(col["type"]).__name__.upper()
        if type_name in ("JSON", "JSONB"):
            return
        # 历史版本可能把逐集优先级建成整数列；重建为 JSON 会清除无法表达逐集状态的旧值。
        op.drop_column(table_name, column_name)
        op.add_column(table_name, sa.Column(column_name, sa.JSON(), nullable=True))
        return


def upgrade() -> None:
    """
    为订阅及完成历史增加逐集洗版优先级，并修复 PostgreSQL 中错误的列类型。
    """
    bind = op.get_bind()
    _ensure_json_column(bind, "subscribe", "episode_priority")
    _ensure_json_column(bind, "subscribehistory", "episode_priority")


def downgrade() -> None:
    """
    移除订阅及完成历史中的逐集洗版优先级字段。
    """
    inspector = sa.inspect(op.get_bind())
    if _has_column(inspector, "subscribehistory", "episode_priority"):
        op.drop_column("subscribehistory", "episode_priority")

    inspector = sa.inspect(op.get_bind())
    if _has_column(inspector, "subscribe", "episode_priority"):
        op.drop_column("subscribe", "episode_priority")
