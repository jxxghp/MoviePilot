"""
2.2.3 添加 downloadhistory.custom_words 字段，用于整理时应用订阅识别词

版本标识： 58edfac72c32
前置版本： 41ef1dd7467c
创建时间： 2026-01-19
"""
from alembic import op
import sqlalchemy as sa

# Alembic 使用以下标识连接迁移版本链。
revision = "58edfac72c32"
down_revision = "41ef1dd7467c"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    为下载历史增加自定义识别词，供后续整理复用订阅线索。
    """
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    dh_columns = inspector.get_columns('downloadhistory')
    if not any(c['name'] == 'custom_words' for c in dh_columns):
        op.add_column('downloadhistory', sa.Column('custom_words', sa.String, nullable=True))


def downgrade() -> None:
    """
    移除下载历史的自定义识别词字段。
    """
    op.drop_column('downloadhistory', 'custom_words')
