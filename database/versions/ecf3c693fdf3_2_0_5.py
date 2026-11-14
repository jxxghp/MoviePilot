"""
2.0.5 补齐订阅历史的识别词、分类和过滤规则，并尝试将站点范围转为 JSON。

版本标识： ecf3c693fdf3
前置版本： a73f2dbf5c09
创建时间： 2024-10-21 12:36:20.631963
"""
import contextlib

from alembic import op
import sqlalchemy as sa

from app.runtime.log import logger


# Alembic 使用以下标识连接迁移版本链。
revision = 'ecf3c693fdf3'
down_revision = 'a73f2dbf5c09'
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    补齐订阅历史的识别词、分类和过滤规则，并尝试将站点范围转为 JSON。
    """
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    table_name = 'subscribehistory'
    columns = inspector.get_columns(table_name)

    try:
        sites_col = next((c for c in columns if c['name'] == 'sites'), None)
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
    """
    本版本不执行回退操作，保留升级后的结构与数据。
    """
    pass
