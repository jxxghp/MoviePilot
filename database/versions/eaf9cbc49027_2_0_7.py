"""2.0.7

Revision ID: eaf9cbc49027
Revises: a295e41830a6
Create Date: 2024-11-16 00:26:09.505188

"""
import contextlib

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'eaf9cbc49027'
down_revision = 'a295e41830a6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    # 检查并添加 site.downloader
    site_columns = inspector.get_columns('site')
    if not any(c['name'] == 'downloader' for c in site_columns):
        op.add_column('site', sa.Column('downloader', sa.String(), nullable=True))

    # 检查并添加 subscribe.downloader
    subscribe_columns = inspector.get_columns('subscribe')
    if not any(c['name'] == 'downloader' for c in subscribe_columns):
        op.add_column('subscribe', sa.Column('downloader', sa.String(), nullable=True))


def downgrade() -> None:
    pass
