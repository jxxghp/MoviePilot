"""2.2.13
为整理历史增加统一媒体数据源与原生ID

Revision ID: e6a1c4b8d2f0
Revises: c4e8f7a1b2d3
Create Date: 2026-07-21
"""

from alembic import op
import sqlalchemy as sa

revision = "e6a1c4b8d2f0"
down_revision = "c4e8f7a1b2d3"
branch_labels = None
depends_on = None


def _has_column(
    inspector: sa.Inspector,
    table_name: str,
    column_name: str,
) -> bool:
    """
    检查数据表是否已存在指定字段。

    :param inspector: SQLAlchemy结构检查器
    :param table_name: 数据表名称
    :param column_name: 字段名称
    :return: 字段是否存在
    """
    if table_name not in inspector.get_table_names():
        return False
    return any(
        column["name"] == column_name
        for column in inspector.get_columns(table_name)
    )


def _has_index(
    inspector: sa.Inspector,
    table_name: str,
    index_name: str,
) -> bool:
    """检查数据表是否已存在指定索引。"""
    if table_name not in inspector.get_table_names():
        return False
    return any(
        index["name"] == index_name
        for index in inspector.get_indexes(table_name)
    )


def _column_names(inspector: sa.Inspector, table_name: str) -> set[str]:
    """读取数据表当前全部字段名。"""
    if table_name not in inspector.get_table_names():
        return set()
    return {
        column["name"]
        for column in inspector.get_columns(table_name)
    }


def _ensure_column_and_index(column_name: str) -> None:
    """独立补齐整理历史的规范身份字段及其索引。"""
    table_name = "transferhistory"
    index_name = f"ix_{table_name}_{column_name}"
    inspector = sa.inspect(op.get_bind())
    if not _has_column(inspector, table_name, column_name):
        op.add_column(
            table_name,
            sa.Column(column_name, sa.String(), nullable=True),
        )

    inspector = sa.inspect(op.get_bind())
    if not _has_index(inspector, table_name, index_name):
        op.create_index(index_name, table_name, [column_name])


def upgrade() -> None:
    """升级整理历史数据源字段。"""
    _ensure_column_and_index("media_source")
    _ensure_column_and_index("media_id")

    columns = _column_names(sa.inspect(op.get_bind()), "transferhistory")
    table_columns = [
        sa.column("media_source", sa.String()),
        sa.column("media_id", sa.String()),
    ]
    if "tmdbid" in columns:
        table_columns.append(sa.column("tmdbid", sa.Integer()))
    if "doubanid" in columns:
        table_columns.append(sa.column("doubanid", sa.String()))
    transfer_history = sa.table("transferhistory", *table_columns)
    connection = op.get_bind()

    if "tmdbid" in columns:
        connection.execute(
            transfer_history.update()
            .where(transfer_history.c.tmdbid.is_not(None))
            .where(transfer_history.c.media_id.is_(None))
            .values(
                media_source="themoviedb",
                media_id=sa.cast(transfer_history.c.tmdbid, sa.String()),
            )
        )

    if "doubanid" not in columns:
        return
    douban_update = (
        transfer_history.update()
        .where(transfer_history.c.doubanid.is_not(None))
        .where(transfer_history.c.media_id.is_(None))
    )
    if "tmdbid" in columns:
        douban_update = douban_update.where(
            transfer_history.c.tmdbid.is_(None)
        )
    connection.execute(
        douban_update.values(
            media_source="douban",
            media_id=transfer_history.c.doubanid,
        )
    )


def downgrade() -> None:
    """回滚整理历史数据源字段。"""
    for column_name in ("media_id", "media_source"):
        index_name = f"ix_transferhistory_{column_name}"
        inspector = sa.inspect(op.get_bind())
        if _has_index(inspector, "transferhistory", index_name):
            op.drop_index(index_name, table_name="transferhistory")

        inspector = sa.inspect(op.get_bind())
        if _has_column(inspector, "transferhistory", column_name):
            op.drop_column("transferhistory", column_name)
