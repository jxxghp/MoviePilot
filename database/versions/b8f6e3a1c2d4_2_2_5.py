"""2.2.5
mediaserveritem 改为按 server + item_id 唯一

Revision ID: b8f6e3a1c2d4
Revises: 93f8cb6a4d1e
Create Date: 2026-05-09
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
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
    return TABLE_NAME in inspector.get_table_names()


def _has_index_signature(inspector: sa.Inspector, unique: bool) -> bool:
    target_columns = tuple(INDEX_COLUMNS)
    for index in inspector.get_indexes(TABLE_NAME):
        if tuple(index.get("column_names") or []) == target_columns and bool(index.get("unique")) == unique:
            return True
    return False


def _drop_index_if_exists(inspector: sa.Inspector) -> None:
    for index in inspector.get_indexes(TABLE_NAME):
        if index.get("name") == INDEX_NAME:
            op.drop_index(INDEX_NAME, table_name=TABLE_NAME)
            return


def _deduplicate_rows() -> None:
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
    inspector = sa.inspect(op.get_bind())
    if not _table_exists(inspector):
        return

    _deduplicate_rows()

    inspector = sa.inspect(op.get_bind())
    if not _has_index_signature(inspector, unique=True):
        op.create_index(INDEX_NAME, TABLE_NAME, INDEX_COLUMNS, unique=True)


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not _table_exists(inspector):
        return

    _drop_index_if_exists(inspector)
