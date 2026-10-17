"""
订阅业务活动与完成历史的稳定关联标识。
"""

from uuid import uuid4

import sqlalchemy as sa
from alembic import op

revision = "e6a4c9f20813"
down_revision = "d9e2a6b4c803"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    为已有活动订阅分配身份，只从升级后的真实业务开始记录。
    """
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    for table in ("subscribe", "subscribehistory"):
        if "activity_key" not in {column["name"] for column in inspector.get_columns(table)}:
            op.add_column(table, sa.Column("activity_key", sa.String(32)))
    subscribe = sa.table("subscribe", sa.column("id", sa.Integer), sa.column("activity_key", sa.String(32)))
    for row in bind.execute(sa.select(subscribe.c.id).where(subscribe.c.activity_key.is_(None))):
        bind.execute(subscribe.update().where(subscribe.c.id == row.id).values(activity_key=uuid4().hex))
    if "subscribeactivity" not in inspector.get_table_names():
        op.create_table(
            "subscribeactivity", sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("activity_key", sa.String(32), nullable=False),
            sa.Column("event_id", sa.String(64), nullable=False, unique=True),
            sa.Column("kind", sa.String(24), nullable=False), sa.Column("source", sa.String(24), nullable=False),
            sa.Column("status", sa.String(24), nullable=False), sa.Column("started_at", sa.String(40), nullable=False),
            sa.Column("finished_at", sa.String(40)), sa.Column("counts", sa.JSON(), nullable=False),
            sa.Column("details", sa.JSON(), nullable=False),
            sa.Column("tracking", sa.JSON(), nullable=False),
        )
        op.create_index("ix_subscribeactivity_key_event", "subscribeactivity", ["activity_key", "event_id"])
        op.create_index("ix_subscribeactivity_key_time", "subscribeactivity", ["activity_key", "started_at", "id"])


def downgrade() -> None:
    """
    移除活动表与身份列，订阅原有业务数据保持可用。
    """
    inspector = sa.inspect(op.get_bind())
    if "subscribeactivity" in inspector.get_table_names():
        op.drop_table("subscribeactivity")
    for table in ("subscribe", "subscribehistory"):
        if "activity_key" in {column["name"] for column in inspector.get_columns(table)}:
            with op.batch_alter_table(table) as batch:
                batch.drop_column("activity_key")
