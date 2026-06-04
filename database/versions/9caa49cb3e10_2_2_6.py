"""2.2.6
为订阅洗版增加按集优先级状态

Revision ID: 9caa49cb3e10
Revises: b8f6e3a1c2d4
Create Date: 2026-05-12
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "9caa49cb3e10"
down_revision = "b8f6e3a1c2d4"
branch_labels = None
depends_on = None


def _has_column(inspector: sa.Inspector, table_name: str, column_name: str) -> bool:
    if table_name not in inspector.get_table_names():
        return False
    return any(column["name"] == column_name for column in inspector.get_columns(table_name))


def _ensure_json_column(bind, table_name: str, column_name: str) -> None:
    """Add the column as JSON, or fix it if it already exists with the wrong type (PostgreSQL only)."""
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
        # Column exists with wrong type (e.g., INTEGER from an intermediate build); replace it.
        # Existing values are non-functional garbage so data loss is acceptable.
        op.drop_column(table_name, column_name)
        op.add_column(table_name, sa.Column(column_name, sa.JSON(), nullable=True))
        return


def upgrade() -> None:
    bind = op.get_bind()
    _ensure_json_column(bind, "subscribe", "episode_priority")
    _ensure_json_column(bind, "subscribehistory", "episode_priority")


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if _has_column(inspector, "subscribehistory", "episode_priority"):
        op.drop_column("subscribehistory", "episode_priority")

    inspector = sa.inspect(op.get_bind())
    if _has_column(inspector, "subscribe", "episode_priority"):
        op.drop_column("subscribe", "episode_priority")
