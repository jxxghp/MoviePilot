"""2.2.16
整理历史按源存储与源路径唯一

Revision ID: 7f5c1d2e3a4b
Revises: f7b2d5c9a301
Create Date: 2026-08-04
"""

from collections import defaultdict

from alembic import op
import sqlalchemy as sa


revision = "7f5c1d2e3a4b"
down_revision = "a8c4e2f6b1d9"
branch_labels = None
depends_on = None

TABLE_NAME = "transferhistory"
INDEX_NAME = "ux_transferhistory_src_storage"
INDEX_COLUMNS = ["src", "src_storage"]

transferhistory = sa.table(
    TABLE_NAME,
    sa.column("id", sa.Integer()),
    sa.column("src", sa.String()),
    sa.column("src_storage", sa.String()),
    sa.column("status", sa.Boolean()),
)


def _table_exists(inspector: sa.Inspector) -> bool:
    """检查整理历史表是否存在。"""
    return TABLE_NAME in inspector.get_table_names()


def _has_unique_index(inspector: sa.Inspector) -> bool:
    """检查源路径与源存储的唯一索引是否已存在。"""
    return any(
        tuple(index.get("column_names") or []) == tuple(INDEX_COLUMNS)
        and bool(index.get("unique"))
        for index in inspector.get_indexes(TABLE_NAME)
    )


def _deduplicate_rows() -> None:
    """归一化旧存储值并按现有查重语义清理重复历史。"""
    bind = op.get_bind()
    bind.execute(
        transferhistory.update()
        .where(
            sa.or_(
                transferhistory.c.src_storage.is_(None),
                transferhistory.c.src_storage == "",
            )
        )
        .values(src_storage="local")
    )

    rows = bind.execute(
        sa.select(
            transferhistory.c.id,
            transferhistory.c.src,
            transferhistory.c.src_storage,
            transferhistory.c.status,
        ).where(transferhistory.c.src.is_not(None))
    ).mappings()
    grouped_rows = defaultdict(list)
    for row in rows:
        grouped_rows[(row["src"], row["src_storage"])].append(row)

    duplicate_ids = []
    for group in grouped_rows.values():
        # 旧运行时在同源混有成功和失败记录时优先返回成功记录；保留其中 ID 最新的一条，
        # 既延续这一保护语义，也让唯一索引能安全建立。
        retained = max(
            group,
            key=lambda row: (bool(row["status"]), row["id"]),
        )
        duplicate_ids.extend(
            row["id"]
            for row in group
            if row["id"] != retained["id"]
        )
    if duplicate_ids:
        bind.execute(
            transferhistory.delete().where(transferhistory.c.id.in_(duplicate_ids))
        )


def _make_src_storage_required(inspector: sa.Inspector) -> None:
    """将源存储设为非空，使唯一索引同样约束本地存储记录。"""
    column = next(
        (
            current
            for current in inspector.get_columns(TABLE_NAME)
            if current["name"] == "src_storage"
        ),
        None,
    )
    if not column or not column.get("nullable"):
        return
    with op.batch_alter_table(TABLE_NAME) as batch_op:
        batch_op.alter_column(
            "src_storage",
            existing_type=sa.String(),
            nullable=False,
            server_default="local",
        )


def upgrade() -> None:
    """归一化并唯一化整理历史的源路径记录。"""
    inspector = sa.inspect(op.get_bind())
    if not _table_exists(inspector):
        return

    _deduplicate_rows()
    _make_src_storage_required(sa.inspect(op.get_bind()))

    inspector = sa.inspect(op.get_bind())
    if not _has_unique_index(inspector):
        op.create_index(INDEX_NAME, TABLE_NAME, INDEX_COLUMNS, unique=True)


def downgrade() -> None:
    """移除整理历史源路径唯一约束。"""
    inspector = sa.inspect(op.get_bind())
    if not _table_exists(inspector):
        return

    if _has_unique_index(inspector):
        op.drop_index(INDEX_NAME, table_name=TABLE_NAME)
    with op.batch_alter_table(TABLE_NAME) as batch_op:
        batch_op.alter_column(
            "src_storage",
            existing_type=sa.String(),
            nullable=True,
            server_default=None,
        )