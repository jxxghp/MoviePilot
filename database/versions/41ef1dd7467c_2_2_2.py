"""2.2.2

Revision ID: 41ef1dd7467c
Revises: a946dae52526
Create Date: 2026-01-13 13:02:41.614029

"""

from alembic import op
from sqlalchemy import text

from app.runtime.log import logger

# revision identifiers, used by Alembic.
revision = "41ef1dd7467c"
down_revision = "a946dae52526"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # systemconfig表 去重
    connection = op.get_bind()

    select_stmt = text(
        """
        SELECT id, key, value
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
            f"已删除重复的 SystemConfig 项：key={row.key}, value={row.value}, id={row.id}"
        )
        delete_stmt = text("DELETE FROM SystemConfig WHERE id = :id")
        connection.execute(delete_stmt, {"id": row.id})

    logger.info("SystemConfig 表去重操作已完成。")


def downgrade() -> None:
    pass
