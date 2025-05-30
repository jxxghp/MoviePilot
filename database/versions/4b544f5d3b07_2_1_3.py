"""2.1.3

Revision ID: 4b544f5d3b07
Revises: 610bb05ddeef
Create Date: 2025-04-03 11:21:42.780337

"""
import contextlib

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import sqlite

# revision identifiers, used by Alembic.
revision = '4b544f5d3b07'
down_revision = '610bb05ddeef'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    with contextlib.suppress(Exception):
        op.add_column('downloadhistory', sa.Column('episode_group', sa.String, nullable=True))
        op.add_column('subscribe', sa.Column('episode_group', sa.String, nullable=True))
        op.add_column('subscribehistory', sa.Column('episode_group', sa.String, nullable=True))
        op.add_column('transferhistory', sa.Column('episode_group', sa.String, nullable=True))
    # ### end Alembic commands ###


def downgrade() -> None:
    pass
