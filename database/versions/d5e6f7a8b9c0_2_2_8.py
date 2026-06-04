"""2.2.8
修复 episode_priority 列类型：PostgreSQL 下若列为 INTEGER 则重建为 JSON

Revision ID: d5e6f7a8b9c0
Revises: 1f0d2c3b4a5e
Create Date: 2026-06-04
"""

from alembic import op
import sqlalchemy as sa

revision = "d5e6f7a8b9c0"
down_revision = "1f0d2c3b4a5e"
branch_labels = None
depends_on = None


def _has_column(inspector: sa.Inspector, table_name: str, column_name: str) -> bool:
    if table_name not in inspector.get_table_names():
        return False
    return any(column["name"] == column_name for column in inspector.get_columns(table_name))


def _fix_episode_priority_type(bind, table_name: str) -> None:
    """On PostgreSQL, if episode_priority exists but is not JSON/JSONB, drop and re-add it as JSON."""
    if bind.dialect.name != "postgresql":
        return
    inspector = sa.inspect(bind)
    if not _has_column(inspector, table_name, "episode_priority"):
        op.add_column(table_name, sa.Column("episode_priority", sa.JSON(), nullable=True))
        return
    for col in inspector.get_columns(table_name):
        if col["name"] != "episode_priority":
            continue
        type_name = type(col["type"]).__name__.upper()
        if type_name in ("JSON", "JSONB"):
            return
        op.drop_column(table_name, "episode_priority")
        op.add_column(table_name, sa.Column("episode_priority", sa.JSON(), nullable=True))
        return


def upgrade() -> None:
    bind = op.get_bind()
    _fix_episode_priority_type(bind, "subscribe")
    _fix_episode_priority_type(bind, "subscribehistory")


def downgrade() -> None:
    pass
