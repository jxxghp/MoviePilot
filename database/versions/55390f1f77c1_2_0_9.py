"""2.0.9

Revision ID: 55390f1f77c1
Revises: bf28a012734c
Create Date: 2024-12-24 13:29:32.225532

"""
import contextlib

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = '55390f1f77c1'
down_revision = 'bf28a012734c'
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    columns = inspector.get_columns('transferhistory')
    if not any(c['name'] == 'downloader' for c in columns):
        op.add_column('transferhistory', sa.Column('downloader', sa.String(), nullable=True))


def downgrade() -> None:
    pass
