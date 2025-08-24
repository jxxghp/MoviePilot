"""2.0.3

Revision ID: e2dbe1421fa4
Revises: 0fb94bf69b38
Create Date: 2024-10-09 13:44:13.926529

"""
import contextlib

from alembic import op
import sqlalchemy as sa

from app.log import logger
from app.db import SessionFactory
from app.db.models import UserConfig

# revision identifiers, used by Alembic.
revision = 'e2dbe1421fa4'
down_revision = '0fb94bf69b38'
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    # 检查并添加 downloadhistory.media_category
    dh_columns = inspector.get_columns('downloadhistory')
    if not any(c['name'] == 'media_category' for c in dh_columns):
        op.add_column('downloadhistory', sa.Column('media_category', sa.String(), nullable=True))

    # 检查并添加 subscribe 表的列
    sub_columns = inspector.get_columns('subscribe')
    if not any(c['name'] == 'custom_words' for c in sub_columns):
        op.add_column('subscribe', sa.Column('custom_words', sa.String(), nullable=True))
    if not any(c['name'] == 'media_category' for c in sub_columns):
        op.add_column('subscribe', sa.Column('media_category', sa.String(), nullable=True))
    if not any(c['name'] == 'filter_groups' for c in sub_columns):
        op.add_column('subscribe', sa.Column('filter_groups', sa.JSON(), nullable=True))

    # 定义需要检查和转换的表和列
    columns_to_alter = {
        'subscribe': 'note',
        'downloadhistory': 'note',
        'mediaserveritem': 'note',
        'message': 'note',
        'plugindata': 'value',
        'site': 'note',
        'sitestatistic': 'note',
        'systemconfig': 'value',
        'userconfig': 'value'
    }

    for table, column_name in columns_to_alter.items():
        try:
            cols = inspector.get_columns(table)
            # 找到对应的列信息
            target_col = next((c for c in cols if c['name'] == column_name), None)
            # 如果列存在且类型不是JSON，则进行修改
            if target_col and not isinstance(target_col['type'], sa.JSON):
                # PostgreSQL需要指定USING子句来处理类型转换
                if conn.dialect.name == 'postgresql':
                    op.alter_column(table, column_name,
                                    existing_type=sa.String(),
                                    type_=sa.JSON(),
                                    postgresql_using=f'"{column_name}"::json')
                else:
                    op.alter_column(table, column_name,
                                    existing_type=sa.String(),
                                    type_=sa.JSON())
        except Exception as e:
            logger.error(f"Could not alter column {column_name} in table {table}: {e}")

    with SessionFactory() as db:
        UserConfig.truncate(db)


def downgrade() -> None:
    pass
