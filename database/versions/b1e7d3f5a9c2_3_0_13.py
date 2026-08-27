"""3.0.13 为整理准入记录增加稳定身份与持久状态。

Revision ID: b1e7d3f5a9c2
Revises: 5f2a9c1e7b4d
Create Date: 2026-08-27
"""

from datetime import datetime
from uuid import NAMESPACE_URL, uuid5

import sqlalchemy as sa
from alembic import op

revision = "b1e7d3f5a9c2"
down_revision = "5f2a9c1e7b4d"
branch_labels = None
depends_on = None

_TABLE_NAME = "transferpending"
_TASK_ID_CONSTRAINT = "uq_transferpending_task_id"
_STATE_CREATED_INDEX = "ix_transferpending_state_created"
_NEW_COLUMNS = {"task_id", "state", "updated_at", "last_error"}


def _column_names() -> set[str]:
    """返回当前待整理登记表的字段集合。"""
    inspector = sa.inspect(op.get_bind())
    if _TABLE_NAME not in inspector.get_table_names():
        return set()
    return {
        column["name"]
        for column in inspector.get_columns(_TABLE_NAME)
    }


def _repair_task_id_constraint() -> None:
    """把稳定任务身份约束修复为 task_id 单列唯一约束。"""
    constraints = {
        constraint.get("name"): tuple(constraint.get("column_names") or ())
        for constraint in sa.inspect(op.get_bind()).get_unique_constraints(
            _TABLE_NAME
        )
        if constraint.get("name")
    }
    current = constraints.get(_TASK_ID_CONSTRAINT)
    if current is not None and current != ("task_id",):
        with op.batch_alter_table(_TABLE_NAME) as batch_op:
            batch_op.drop_constraint(_TASK_ID_CONSTRAINT, type_="unique")
        current = None
    if current is None:
        with op.batch_alter_table(_TABLE_NAME) as batch_op:
            batch_op.create_unique_constraint(
                _TASK_ID_CONSTRAINT,
                ["task_id"],
            )


def _repair_state_created_index() -> None:
    """按列顺序和非唯一语义修复恢复扫描索引。"""
    indexes = {
        index.get("name"): index
        for index in sa.inspect(op.get_bind()).get_indexes(_TABLE_NAME)
        if index.get("name")
    }
    current = indexes.get(_STATE_CREATED_INDEX)
    if current is not None and (
            tuple(current.get("column_names") or ()) != ("state", "created_at", "id")
            or bool(current.get("unique"))
    ):
        op.drop_index(_STATE_CREATED_INDEX, table_name=_TABLE_NAME)
        current = None
    if current is None:
        op.create_index(
            _STATE_CREATED_INDEX,
            _TABLE_NAME,
            ["state", "created_at", "id"],
            unique=False,
        )


def _backfill_admission_state() -> None:
    """为旧登记保守生成稳定身份、接纳状态与更新时间。"""
    pending = sa.table(
        _TABLE_NAME,
        sa.column("id", sa.Integer()),
        sa.column("storage", sa.String()),
        sa.column("src_path", sa.String()),
        sa.column("created_at", sa.String()),
        sa.column("task_id", sa.String()),
        sa.column("state", sa.String()),
        sa.column("updated_at", sa.String()),
    )
    connection = op.get_bind()
    rows = connection.execute(
        sa.select(
            pending.c.id,
            pending.c.storage,
            pending.c.src_path,
            pending.c.created_at,
            pending.c.task_id,
            pending.c.state,
            pending.c.updated_at,
        )
    ).mappings().all()
    fallback_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for row in rows:
        task_id = row["task_id"] or uuid5(
            NAMESPACE_URL,
            (
                "moviepilot:transferpending:"
                f"{row['id']}:{row['storage']}:{row['src_path']}"
            ),
        ).hex
        values = {
            "task_id": task_id,
            "state": row["state"] or "accepted",
            "updated_at": (
                row["updated_at"] or row["created_at"] or fallback_time
            ),
        }
        connection.execute(
            pending.update()
            .where(pending.c.id == row["id"])
            .values(**values)
        )


def upgrade() -> None:
    """增加准入状态字段，回填旧行并建立稳定任务标识约束。"""
    columns = _column_names()
    if not columns:
        return
    if "task_id" not in columns:
        op.add_column(
            _TABLE_NAME,
            sa.Column("task_id", sa.String(length=64), nullable=True),
        )
    if "state" not in columns:
        op.add_column(
            _TABLE_NAME,
            sa.Column("state", sa.String(length=32), nullable=True),
        )
    if "updated_at" not in columns:
        op.add_column(
            _TABLE_NAME,
            sa.Column("updated_at", sa.String(length=40), nullable=True),
        )
    if "last_error" not in columns:
        op.add_column(
            _TABLE_NAME,
            sa.Column("last_error", sa.Text(), nullable=True),
        )

    _backfill_admission_state()
    with op.batch_alter_table(_TABLE_NAME) as batch_op:
        batch_op.alter_column(
            "task_id", existing_type=sa.String(length=64), nullable=False
        )
        batch_op.alter_column(
            "state", existing_type=sa.String(length=32), nullable=False
        )
        batch_op.alter_column(
            "updated_at", existing_type=sa.String(length=40), nullable=False
        )
    _repair_task_id_constraint()
    _repair_state_created_index()


def downgrade() -> None:
    """移除准入状态字段并保留旧版可识别的登记事实。"""
    columns = _column_names()
    if not columns or not (_NEW_COLUMNS & columns):
        return
    index_names = {
        index.get("name")
        for index in sa.inspect(op.get_bind()).get_indexes(_TABLE_NAME)
    }
    if _STATE_CREATED_INDEX in index_names:
        op.drop_index(_STATE_CREATED_INDEX, table_name=_TABLE_NAME)
    constraint_names = {
        constraint.get("name")
        for constraint in sa.inspect(op.get_bind()).get_unique_constraints(
            _TABLE_NAME
        )
    }
    with op.batch_alter_table(_TABLE_NAME) as batch_op:
        if "task_id" in columns and _TASK_ID_CONSTRAINT in constraint_names:
            batch_op.drop_constraint(_TASK_ID_CONSTRAINT, type_="unique")
        for column_name in ("last_error", "updated_at", "state", "task_id"):
            if column_name in columns:
                batch_op.drop_column(column_name)
