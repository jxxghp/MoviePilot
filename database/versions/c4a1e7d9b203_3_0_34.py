"""3.0.34 增加艺术家完整作品获取任务。"""

import sqlalchemy as sa
from alembic import op

revision = "c4a1e7d9b203"
down_revision = "b2d4f6a8c1e3"
branch_labels = None
depends_on = None


def _id_column(dialect_name: str) -> sa.Column:
    if dialect_name == "postgresql":
        return sa.Column("id", sa.Integer(), sa.Identity(start=1, cycle=True), primary_key=True)
    return sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True)


def upgrade() -> None:
    bind = op.get_bind()
    if "musicartistacquisition" not in sa.inspect(bind).get_table_names():
        op.create_table(
            "musicartistacquisition",
            _id_column(bind.dialect.name),
            sa.Column("job_id", sa.String(64), nullable=False),
            sa.Column("plan_key", sa.String(64), nullable=False),
            sa.Column("username", sa.String(255), nullable=False),
            sa.Column("artist_source", sa.String(64), nullable=False),
            sa.Column("artist_id", sa.String(128), nullable=False),
            sa.Column("artist_name", sa.String(255), nullable=False),
            sa.Column("state", sa.String(32), nullable=False),
            sa.Column("scope", sa.JSON(), nullable=False),
            sa.Column("plan", sa.JSON(), nullable=False),
            sa.Column("results", sa.JSON(), nullable=False),
            sa.Column("total_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("covered_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("supplement_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("failed_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.String(40), nullable=False),
            sa.Column("updated_at", sa.String(40), nullable=False),
            sa.Column("finished_at", sa.String(40)),
            sa.Column("last_error", sa.Text()),
        )
    existing = {item["name"] for item in sa.inspect(bind).get_indexes("musicartistacquisition")}
    for name, columns, unique in (
        ("ix_musicartistacquisition_job_id", ["job_id"], True),
        ("ix_musicartistacquisition_plan_identity", ["username", "plan_key"], True),
        ("ix_musicartistacquisition_artist_updated", ["artist_source", "artist_id", "updated_at", "id"], False),
    ):
        if name not in existing:
            op.create_index(name, "musicartistacquisition", columns, unique=unique)


def downgrade() -> None:
    if "musicartistacquisition" in sa.inspect(op.get_bind()).get_table_names():
        op.drop_table("musicartistacquisition")
