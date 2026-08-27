"""3.0.15 为整理恢复任务增加强 CAS 租约字段。

Revision ID: d3a9e5f7b2c4
Revises: c2f8a4d6e1b3
Create Date: 2026-08-27
"""

import sqlalchemy as sa
from alembic import op

revision = "d3a9e5f7b2c4"
down_revision = "c2f8a4d6e1b3"
branch_labels = None
depends_on = None

_TABLE_NAME = "transferpending"
_LEASE_INDEX = "ix_transferpending_recovery_lease"
_LEASE_COLUMNS = {
    "lease_owner",
    "lease_token",
    "lease_expires_at",
    "heartbeat_at",
    "attempt_count",
}


def _column_names() -> set[str]:
    """返回当前待整理登记表的字段集合。"""
    inspector = sa.inspect(op.get_bind())
    if _TABLE_NAME not in inspector.get_table_names():
        return set()
    return {
        column["name"]
        for column in inspector.get_columns(_TABLE_NAME)
    }


def _index_names() -> set[str]:
    """返回当前待整理登记表的索引名称集合。"""
    inspector = sa.inspect(op.get_bind())
    if _TABLE_NAME not in inspector.get_table_names():
        return set()
    return {
        index["name"]
        for index in inspector.get_indexes(_TABLE_NAME)
        if index.get("name")
    }


def upgrade() -> None:
    """增加租约身份、到期、心跳和接管次数，并支持中断后重跑。"""
    columns = _column_names()
    if not columns:
        return
    additions = (
        (
            "lease_owner",
            sa.Column("lease_owner", sa.String(length=128), nullable=True),
        ),
        (
            "lease_token",
            sa.Column("lease_token", sa.String(length=64), nullable=True),
        ),
        (
            "lease_expires_at",
            sa.Column("lease_expires_at", sa.String(length=40), nullable=True),
        ),
        (
            "heartbeat_at",
            sa.Column("heartbeat_at", sa.String(length=40), nullable=True),
        ),
        (
            "attempt_count",
            sa.Column("attempt_count", sa.Integer(), nullable=True),
        ),
    )
    for column_name, column in additions:
        if column_name not in columns:
            op.add_column(_TABLE_NAME, column)

    columns = _column_names()
    if "attempt_count" in columns:
        pending = sa.table(
            _TABLE_NAME,
            sa.column("attempt_count", sa.Integer()),
        )
        op.get_bind().execute(
            pending.update()
            .where(pending.c.attempt_count.is_(None))
            .values(attempt_count=0)
        )
        with op.batch_alter_table(_TABLE_NAME) as batch_op:
            batch_op.alter_column(
                "attempt_count",
                existing_type=sa.Integer(),
                nullable=False,
            )

    if _LEASE_INDEX not in _index_names():
        op.create_index(
            _LEASE_INDEX,
            _TABLE_NAME,
            ["state", "lease_expires_at", "created_at", "id"],
            unique=False,
        )


def downgrade() -> None:
    """移除租约索引和字段，保留原业务状态及规划检查点。"""
    columns = _column_names()
    if not columns or not (_LEASE_COLUMNS & columns):
        return
    if _LEASE_INDEX in _index_names():
        op.drop_index(_LEASE_INDEX, table_name=_TABLE_NAME)
    with op.batch_alter_table(_TABLE_NAME) as batch_op:
        for column_name in (
                "attempt_count",
                "heartbeat_at",
                "lease_expires_at",
                "lease_token",
                "lease_owner",
        ):
            if column_name in columns:
                batch_op.drop_column(column_name)
