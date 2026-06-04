"""2.2.9
为工作流增加执行配置和结构化执行状态

Revision ID: 7c1a2b3d4e5f
Revises: d5e6f7a8b9c0
Create Date: 2026-06-04
"""

from alembic import op
import sqlalchemy as sa

revision = "7c1a2b3d4e5f"
down_revision = "d5e6f7a8b9c0"
branch_labels = None
depends_on = None


def _has_column(inspector: sa.Inspector, table_name: str, column_name: str) -> bool:
    """检查数据表是否已存在指定列。"""
    if table_name not in inspector.get_table_names():
        return False
    return any(column["name"] == column_name for column in inspector.get_columns(table_name))


def _add_json_column_if_missing(table_name: str, column_name: str) -> None:
    """缺失时为数据表新增 JSON 列。"""
    inspector = sa.inspect(op.get_bind())
    if not _has_column(inspector, table_name, column_name):
        op.add_column(table_name, sa.Column(column_name, sa.JSON(), nullable=True))


def upgrade() -> None:
    """升级数据库结构。"""
    _add_json_column_if_missing("workflow", "execution_config")
    _add_json_column_if_missing("workflow", "execution_state")


def downgrade() -> None:
    """回滚数据库结构。"""
    inspector = sa.inspect(op.get_bind())
    if _has_column(inspector, "workflow", "execution_state"):
        op.drop_column("workflow", "execution_state")
    inspector = sa.inspect(op.get_bind())
    if _has_column(inspector, "workflow", "execution_config"):
        op.drop_column("workflow", "execution_config")
