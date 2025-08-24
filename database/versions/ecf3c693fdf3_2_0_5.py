"""2.0.5

Revision ID: ecf3c693fdf3
Revises: a73f2dbf5c09
Create Date: 2024-10-21 12:36:20.631963

"""
import contextlib

from alembic import op
import sqlalchemy as sa

from app.log import logger


# revision identifiers, used by Alembic.
revision = 'ecf3c693fdf3'
down_revision = 'a73f2dbf5c09'
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    table_name = 'subscribehistory'
    columns = inspector.get_columns(table_name)

    try:
        sites_col = next((c for c in columns if c['name'] == 'sites'), None)
        # 如果 'sites' 列存在且类型不是 JSON，则进行修改
        if sites_col and not isinstance(sites_col['type'], sa.JSON):
            if conn.dialect.name == 'postgresql':
                op.alter_column(table_name, 'sites',
                                existing_type=sa.String(),
                                type_=sa.JSON(),
                                postgresql_using='sites::json')
            else:
                op.alter_column(table_name, 'sites',
                                existing_type=sa.String(),
                                type_=sa.JSON())
    except Exception as e:
        logger.error(f"Could not alter column 'sites' in table {table_name}: {e}")

    if not any(c['name'] == 'custom_words' for c in columns):
        op.add_column(table_name, sa.Column('custom_words', sa.String(), nullable=True))

    if not any(c['name'] == 'media_category' for c in columns):
        op.add_column(table_name, sa.Column('media_category', sa.String(), nullable=True))

    if not any(c['name'] == 'filter_groups' for c in columns):
        op.add_column(table_name, sa.Column('filter_groups', sa.JSON(), nullable=True))


def downgrade() -> None:
    pass
