"""2.1.6

Revision ID: 3df653756eec
Revises: 486e56a62dcb
Create Date: 2025-06-11 19:52:57.185355

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '3df653756eec'
down_revision = '486e56a62dcb'
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    user = sa.table(
        "user",
        sa.column("id", sa.Integer()),
        sa.column("is_superuser", sa.Boolean()),
        sa.column("permissions", sa.JSON()),
    )
    users = connection.execute(
        sa.select(user.c.id, user.c.is_superuser, user.c.permissions)
    ).mappings().all()
    permissions = {
        "discovery": True,
        "search": True,
        "subscribe": True,
        "manage": False,
    }
    for item in users:
        if item["is_superuser"] or item["permissions"]:
            continue
        connection.execute(
            user.update()
            .where(user.c.id == item["id"])
            .values(permissions=permissions)
        )


def downgrade() -> None:
    pass
