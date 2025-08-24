"""2.1.8

Revision ID: 4666ce24a443
Revises: 3891a5e722a1
Create Date: 2025-07-22 13:54:04.196126

"""
import contextlib

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = '4666ce24a443'
down_revision = '3891a5e722a1'
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    columns = inspector.get_columns('workflow')

    if not any(c['name'] == 'trigger_type' for c in columns):
        op.add_column('workflow', sa.Column('trigger_type', sa.String(), nullable=True, default='timer'))

    if not any(c['name'] == 'event_type' for c in columns):
        op.add_column('workflow', sa.Column('event_type', sa.String(), nullable=True))

    if not any(c['name'] == 'event_conditions' for c in columns):
        op.add_column('workflow', sa.Column('event_conditions', sa.JSON(), nullable=True, default={}))


def downgrade() -> None:
    pass
