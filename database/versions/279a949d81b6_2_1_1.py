"""
2.1.1 装配缓存依赖并清理旧种子缓存，避免继续读取旧格式。

版本标识： 279a949d81b6
前置版本： ca5461f314f2
创建时间： 2025-02-14 19:02:24.989349
"""

from app.application.torrent.download import clear_torrent_cache
from app.startup.initializers.cache import configure_cache_dependencies

# Alembic 使用以下标识连接迁移版本链。
revision = '279a949d81b6'
down_revision = 'ca5461f314f2'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 迁移执行时生命周期尚未装配，按启动缓存入口装配后清理一次缓存。
    """
    装配缓存依赖并清理旧种子缓存，避免继续读取旧格式。
    """
    configure_cache_dependencies()
    clear_torrent_cache()


def downgrade() -> None:
    """
    本版本不执行回退操作，保留升级后的结构与数据。
    """
    pass
