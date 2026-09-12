"""3.0.35 持久化目录整理批次。"""

import sqlalchemy as sa
from alembic import op

revision = "d6f4b2a9c813"
down_revision = "c4a1e7d9b203"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {item["name"] for item in inspector.get_columns("transferhistory")}
    for name, column_type in (
        ("transfer_batch_id", sa.String(64)),
        ("transfer_batch_title", sa.String()),
        ("transfer_batch_root", sa.String()),
        ("transfer_batch_total", sa.Integer()),
    ):
        if name not in columns:
            op.add_column("transferhistory", sa.Column(name, column_type, nullable=True))
    indexes = {item["name"] for item in sa.inspect(bind).get_indexes("transferhistory")}
    if "ix_transferhistory_transfer_batch_id" not in indexes:
        op.create_index(
            "ix_transferhistory_transfer_batch_id",
            "transferhistory",
            ["transfer_batch_id"],
            unique=False,
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    indexes = {item["name"] for item in inspector.get_indexes("transferhistory")}
    if "ix_transferhistory_transfer_batch_id" in indexes:
        op.drop_index("ix_transferhistory_transfer_batch_id", table_name="transferhistory")
    columns = {item["name"] for item in sa.inspect(bind).get_columns("transferhistory")}
    for name in (
        "transfer_batch_total",
        "transfer_batch_root",
        "transfer_batch_title",
        "transfer_batch_id",
    ):
        if name in columns:
            op.drop_column("transferhistory", name)
