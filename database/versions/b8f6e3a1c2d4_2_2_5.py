"""
2.2.5 mediaserveritem 改为按 server + item_id 唯一

版本标识： b8f6e3a1c2d4
前置版本： 93f8cb6a4d1e
创建时间： 2026-05-09
"""

from alembic import op
import sqlalchemy as sa

# Alembic 使用以下标识连接迁移版本链。
revision = "b8f6e3a1c2d4"
down_revision = "93f8cb6a4d1e"
branch_labels = None
depends_on = None

TABLE_NAME = "mediaserveritem"
INDEX_NAME = "ux_mediaserveritem_server_item_id"
INDEX_COLUMNS = ["server", "item_id"]

mediaserveritem = sa.table(
    TABLE_NAME,
    sa.column("id", sa.Integer),
    sa.column("server", sa.String),
    sa.column("item_id", sa.String),
)


def _table_exists(inspector: sa.Inspector) -> bool:
    """
    检查本次迁移的目标表是否存在。
    """
    return TABLE_NAME in inspector.get_table_names()


def _has_index_signature(inspector: sa.Inspector, unique: bool) -> bool:
    """
    按字段顺序和唯一性确认是否已存在等价索引。
    """
    target_columns = tuple(INDEX_COLUMNS)
    for index in inspector.get_indexes(TABLE_NAME):
        if tuple(index.get("column_names") or []) == target_columns and bool(index.get("unique")) == unique:
            return True
    return False


def _drop_index_if_exists(inspector: sa.Inspector) -> None:
    """
    仅删除本次迁移命名的索引，不改动其他索引。
    """
    for index in inspector.get_indexes(TABLE_NAME):
        if index.get("name") == INDEX_NAME:
            op.drop_index(INDEX_NAME, table_name=TABLE_NAME)
            return


def _deduplicate_rows() -> None:
    """
    对非空服务器与条目标识分组保留最大 ID，删除其余重复记录。
    """
    bind = op.get_bind()
    keep_ids = (
        sa.select(sa.func.max(mediaserveritem.c.id))
        .where(
            mediaserveritem.c.server.is_not(None),
            mediaserveritem.c.item_id.is_not(None),
        )
        .group_by(mediaserveritem.c.server, mediaserveritem.c.item_id)
    )
    bind.execute(
        mediaserveritem.delete().where(
            sa.and_(
                mediaserveritem.c.server.is_not(None),
                mediaserveritem.c.item_id.is_not(None),
                mediaserveritem.c.id.not_in(keep_ids),
            )
        )
    )


def upgrade() -> None:
    """
    清理媒体库重复条目，并建立服务器与条目标识的联合唯一索引。
    """
    inspector = sa.inspect(op.get_bind())
    if not _table_exists(inspector):
        return

    _deduplicate_rows()

    inspector = sa.inspect(op.get_bind())
    if not _has_index_signature(inspector, unique=True):
        op.create_index(INDEX_NAME, TABLE_NAME, INDEX_COLUMNS, unique=True)


def downgrade() -> None:
    """
    删除本版本命名的媒体库联合唯一索引，不恢复已清理的重复行。
    """
    inspector = sa.inspect(op.get_bind())
    if not _table_exists(inspector):
        return

    _drop_index_if_exists(inspector)
