"""
2.1.9 保留本版本的迁移链节点，不修改数据库结构或数据。

版本标识： d58298a0879f
前置版本： 4666ce24a443
创建时间： 2025-08-19 11:56:39.652032
"""

# Alembic 使用以下标识连接迁移版本链。
revision = 'd58298a0879f'
down_revision = '4666ce24a443'
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    保留本版本的迁移链节点，不修改数据库结构或数据。
    """
    pass


def downgrade() -> None:
    """
    本版本不执行回退操作，保留升级后的结构与数据。
    """
    pass
