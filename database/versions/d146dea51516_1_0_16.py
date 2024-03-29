"""1.0.16

Revision ID: d146dea51516
Revises: 5813aaa7cb3a
Create Date: 2024-03-18 18:13:38.099531

"""
import contextlib

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd146dea51516'
down_revision = '5813aaa7cb3a'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    with contextlib.suppress(Exception):
        with op.batch_alter_table("subscribe") as batch_op:
            batch_op.add_column(sa.Column('bangumiid', sa.Integer, nullable=True))
    try:
        op.create_index('ix_subscribe_bangumiid', 'subscribe', ['bangumiid'], unique=False)
    except Exception as err:
        pass
    # ### end Alembic commands ###


def downgrade() -> None:
    pass
