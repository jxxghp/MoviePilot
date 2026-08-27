"""3.0.4
新增待整理文件登记表

整理队列是纯内存的 queue.Queue，进程重启会让队列里的任务连同「这些文件还没
整理」这个事实一起蒸发，而已稳定落地的文件不会再产生任何监控事件，等于永久
漏件。该表只登记「存储 + 源文件路径」这一最小事实，供启动时回放。

Revision ID: e3d9f4b7c806
Revises: 7f5c1d2e3a4b
Create Date: 2026-08-10
"""

import sqlalchemy as sa
from alembic import op

revision = "e3d9f4b7c806"
down_revision = "7f5c1d2e3a4b"
branch_labels = None
depends_on = None

_TABLE_NAME = "transferpending"
_INDEX_NAME = "ux_transferpending_storage_path"
_EXPECTED_COLUMNS = {"id", "storage", "src_path", "created_at"}


def _has_table(table_name: str) -> bool:
    """检查数据表是否已存在。"""
    inspector = sa.inspect(op.get_bind())
    return table_name in inspector.get_table_names()


def _table_row_count() -> int:
    """返回待整理表行数，用于判断残缺结构能否无损重建。"""
    return op.get_bind().execute(
        sa.select(sa.func.count()).select_from(sa.table(_TABLE_NAME))
    ).scalar_one()


def _create_table() -> None:
    """创建 3.0.4 定义的完整待整理登记表。"""
    op.create_table(
        _TABLE_NAME,
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("storage", sa.String, nullable=False),
        sa.Column("src_path", sa.String, nullable=False),
        sa.Column("created_at", sa.String),
    )


def _validate_or_recreate_table() -> None:
    """校验中断升级留下的表结构，仅允许空残表自动重建。"""
    inspector = sa.inspect(op.get_bind())
    columns = inspector.get_columns(_TABLE_NAME)
    column_names = {column["name"] for column in columns}
    nullable = {
        column["name"]
        for column in columns
        if column.get("nullable", True)
    }
    primary_key = tuple(
        inspector.get_pk_constraint(_TABLE_NAME).get("constrained_columns") or ()
    )
    column_types = {
        column["name"]: column["type"]
        for column in columns
    }
    wrong_types = (
        not isinstance(column_types.get("id"), sa.Integer)
        or any(
            not isinstance(column_types.get(column_name), sa.String)
            for column_name in ("storage", "src_path", "created_at")
        )
    )
    malformed = (
        column_names != _EXPECTED_COLUMNS
        or nullable != {"created_at"}
        or primary_key != ("id",)
        or wrong_types
    )
    if not malformed:
        return
    if _table_row_count() > 0:
        raise RuntimeError(
            "检测到含数据的不完整 transferpending 表，"
            "无法自动恢复 3.0.4 迁移"
        )
    op.drop_table(_TABLE_NAME)
    _create_table()


def _repair_storage_path_index() -> None:
    """把源身份索引修复为指定列上的唯一索引。"""
    indexes = {
        index["name"]: index
        for index in sa.inspect(op.get_bind()).get_indexes(_TABLE_NAME)
        if index.get("name")
    }
    current = indexes.get(_INDEX_NAME)
    if current is not None and (
            tuple(current.get("column_names") or ()) != ("storage", "src_path")
            or not current.get("unique")
    ):
        op.drop_index(_INDEX_NAME, table_name=_TABLE_NAME)
        current = None
    if current is None:
        op.create_index(
            _INDEX_NAME,
            _TABLE_NAME,
            ["storage", "src_path"],
            unique=True,
        )


def upgrade() -> None:
    """
    创建待整理文件登记表。
    """
    if not _has_table(_TABLE_NAME):
        _create_table()
    else:
        _validate_or_recreate_table()
    _repair_storage_path_index()


def downgrade() -> None:
    """
    删除待整理文件登记表。
    """
    if not _has_table(_TABLE_NAME):
        return
    index_names = {
        index["name"]
        for index in sa.inspect(op.get_bind()).get_indexes(_TABLE_NAME)
        if index.get("name")
    }
    if _INDEX_NAME in index_names:
        op.drop_index(_INDEX_NAME, table_name=_TABLE_NAME)
    op.drop_table(_TABLE_NAME)
