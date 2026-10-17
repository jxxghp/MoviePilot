"""
2.0.3 补齐分类、识别词和过滤规则字段，尝试修复 JSON 列并清空旧用户配置。

版本标识： e2dbe1421fa4
前置版本： 0fb94bf69b38
创建时间： 2024-10-09 13:44:13.926529
"""
from alembic import op
import sqlalchemy as sa

from app.runtime.log import logger

# Alembic 使用以下标识连接迁移版本链。
revision = 'e2dbe1421fa4'
down_revision = '0fb94bf69b38'
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    补齐分类、识别词和过滤规则字段，尝试修复 JSON 列并清空旧用户配置。
    """
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    dh_columns = inspector.get_columns('downloadhistory')
    if not any(c['name'] == 'media_category' for c in dh_columns):
        op.add_column('downloadhistory', sa.Column('media_category', sa.String(), nullable=True))

    sub_columns = inspector.get_columns('subscribe')
    if not any(c['name'] == 'custom_words' for c in sub_columns):
        op.add_column('subscribe', sa.Column('custom_words', sa.String(), nullable=True))
    if not any(c['name'] == 'media_category' for c in sub_columns):
        op.add_column('subscribe', sa.Column('media_category', sa.String(), nullable=True))
    if not any(c['name'] == 'filter_groups' for c in sub_columns):
        op.add_column('subscribe', sa.Column('filter_groups', sa.JSON(), nullable=True))

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
            target_col = next((c for c in cols if c['name'] == column_name), None)
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

    conn.execute(sa.delete(sa.table("userconfig")))


def downgrade() -> None:
    """
    本版本不执行回退操作，保留升级后的结构与数据。
    """
    pass
