"""3.0.11 add retention cleanup indexes.

Revision ID: a6c8e2f4b1d3
Revises: e4f7a1b2c3d5
Create Date: 2026-08-26
"""

import sqlalchemy as sa
from alembic import op


revision = "a6c8e2f4b1d3"
down_revision = "e4f7a1b2c3d5"
branch_labels = None
depends_on = None


_CLEANUP_INDEXES = {
    "subscribehistory": (
        "ix_subscribehistory_date_id",
        ("date", "id"),
    ),
    "agentchat": (
        "ix_agentchat_updated_id",
        ("updated_at", "id"),
    ),
    "agenttaskrun": (
        "ix_agenttaskrun_status_started_id",
        ("status", "started_at", "id"),
    ),
}


def _index_names(table_name: str) -> set[str]:
    """读取实时索引名，兼容 fresh metadata 已创建当前索引的路径。"""
    return {
        index["name"]
        for index in sa.inspect(op.get_bind()).get_indexes(table_name)
    }


def upgrade() -> None:
    """为三张无界增长历史表补充保留期扫描索引。"""
    table_names = set(sa.inspect(op.get_bind()).get_table_names())
    for table_name, (index_name, columns) in _CLEANUP_INDEXES.items():
        if table_name not in table_names or index_name in _index_names(table_name):
            continue
        op.create_index(index_name, table_name, list(columns), unique=False)


def downgrade() -> None:
    """删除保留期扫描索引，不改动任何历史数据。"""
    table_names = set(sa.inspect(op.get_bind()).get_table_names())
    for table_name, (index_name, _columns) in _CLEANUP_INDEXES.items():
        if table_name in table_names and index_name in _index_names(table_name):
            op.drop_index(index_name, table_name=table_name)
