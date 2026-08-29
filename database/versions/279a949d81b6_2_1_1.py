"""2.1.1

Revision ID: 279a949d81b6
Revises: ca5461f314f2
Create Date: 2025-02-14 19:02:24.989349

"""

from app.adapters.cache.backends import configure_platform_cache
from app.application.torrent.download import clear_torrent_cache

# revision identifiers, used by Alembic.
revision = '279a949d81b6'
down_revision = 'ca5461f314f2'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 迁移执行时生命周期尚未装配，直接通过缓存端口清理一次缓存。
    configure_platform_cache()
    clear_torrent_cache()


def downgrade() -> None:
    pass
