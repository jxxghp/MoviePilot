"""
2.0.7 为站点和活动订阅补齐可空的下载器字段。

版本标识： eaf9cbc49027
前置版本： a295e41830a6
创建时间： 2024-11-16 00:26:09.505188
"""
import contextlib

from alembic import op
import sqlalchemy as sa

# Alembic 使用以下标识连接迁移版本链。
revision = 'eaf9cbc49027'
down_revision = 'a295e41830a6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    为站点和活动订阅补齐可空的下载器字段。
    """
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    site_columns = inspector.get_columns('site')
    if not any(c['name'] == 'downloader' for c in site_columns):
        op.add_column('site', sa.Column('downloader', sa.String(), nullable=True))

    subscribe_columns = inspector.get_columns('subscribe')
    if not any(c['name'] == 'downloader' for c in subscribe_columns):
        op.add_column('subscribe', sa.Column('downloader', sa.String(), nullable=True))


def downgrade() -> None:
    """
    本版本不执行回退操作，保留升级后的结构与数据。
    """
    pass
