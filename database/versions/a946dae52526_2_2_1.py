"""
2.2.1 将 PostgreSQL 站点用户标识转换为文本。

版本标识： a946dae52526
前置版本： 5b3355c964bb
创建时间： 2025-08-20 17:50:00.000000
"""
import sqlalchemy as sa
from alembic import op

from app.runtime.log import logger
from app.runtime.config import settings

# Alembic 使用以下标识连接迁移版本链。
revision = 'a946dae52526'
down_revision = '5b3355c964bb'
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    仅在 PostgreSQL 上将站点用户标识从整数列迁移为文本列。
    """
    connection = op.get_bind()

    if settings.DB_TYPE.lower() == "postgresql":
        migrate_postgresql_userid(connection)


def downgrade() -> None:
    """
    本版本不执行类型回退，保留站点用户标识的文本值。
    """
    pass


def migrate_postgresql_userid(connection):
    """
    通过临时文本列转换站点用户标识，成功后替换旧列。

    :param connection: Alembic 当前迁移事务连接
    :raises Exception: 建列、转换或替换失败时记录错误并向上传播
    """
    try:
        logger.info("开始PostgreSQL数据库userid字段迁移...")

        connection.execute(sa.text("""
            ALTER TABLE siteuserdata 
            ADD COLUMN userid_new VARCHAR
        """))

        connection.execute(sa.text("""
            UPDATE siteuserdata 
            SET userid_new = CAST(userid AS VARCHAR)
            WHERE userid IS NOT NULL
        """))

        connection.execute(sa.text("""
            ALTER TABLE siteuserdata 
            DROP COLUMN userid
        """))

        connection.execute(sa.text("""
            ALTER TABLE siteuserdata 
            RENAME COLUMN userid_new TO userid
        """))

        logger.info("PostgreSQL数据库userid字段迁移完成")

    except Exception as e:
        logger.error(f"PostgreSQL数据库userid字段迁移失败: {e}")
        raise





