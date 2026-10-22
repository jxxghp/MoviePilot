"""
2.2.2 清理重复系统配置，每个配置键保留最大 ID 对应记录。

版本标识： 41ef1dd7467c
前置版本： a946dae52526
创建时间： 2026-01-13 13:02:41.614029
"""

from alembic import op
from sqlalchemy import text

from app.runtime.log import logger

# Alembic 使用以下标识连接迁移版本链。
revision = "41ef1dd7467c"
down_revision = "a946dae52526"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    清理重复系统配置，每个配置键保留最大 ID 对应记录。
    """
    connection = op.get_bind()

    select_stmt = text(
        """
        SELECT id, key
        FROM SystemConfig
        WHERE id NOT IN (
            SELECT MAX(id)
            FROM SystemConfig
            GROUP BY key
        )
    """
    )
    to_delete = connection.execute(select_stmt).fetchall()
    for row in to_delete:
        logger.warn(
            f"已删除重复的 SystemConfig 项：key={row.key}, id={row.id}"
        )
        delete_stmt = text("DELETE FROM SystemConfig WHERE id = :id")
        connection.execute(delete_stmt, {"id": row.id})

    logger.info("SystemConfig 表去重操作已完成。")


def downgrade() -> None:
    """
    本版本不执行回退操作，保留升级后的结构与数据。
    """
    pass
