"""2.0.8

Revision ID: bf28a012734c
Revises: eaf9cbc49027
Create Date: 2024-12-23 18:29:31.202143

"""
import contextlib

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = 'bf28a012734c'
down_revision = 'eaf9cbc49027'
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    columns = inspector.get_columns('downloadhistory')
    if not any(c['name'] == 'downloader' for c in columns):
        op.add_column('downloadhistory', sa.Column('downloader', sa.String(), nullable=True))


def downgrade() -> None:
    pass
