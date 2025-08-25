"""2.2.1

Revision ID: a946dae52526
Revises: 5b3355c964bb
Create Date: 2025-08-20 17:50:00.000000

"""
import sqlalchemy as sa
from alembic import op

from app.log import logger
from app.core.config import settings

# revision identifiers, used by Alembic.
revision = 'a946dae52526'
down_revision = '5b3355c964bb'
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    升级：将SiteUserData表的userid字段从Integer改为String
    """
    connection = op.get_bind()
    
    if settings.DB_TYPE.lower() == "postgresql":
        # PostgreSQL数据库迁移
        migrate_postgresql_userid(connection)


def downgrade() -> None:
    """
    降级：将SiteUserData表的userid字段从String改回Integer
    """
    pass


def migrate_postgresql_userid(connection):
    """
    PostgreSQL数据库userid字段迁移
    """
    try:
        logger.info("开始PostgreSQL数据库userid字段迁移...")
        
        # 1. 创建临时列
        connection.execute(sa.text("""
            ALTER TABLE siteuserdata 
            ADD COLUMN userid_new VARCHAR
        """))
        
        # 2. 将现有数据转换为字符串并复制到新列
        connection.execute(sa.text("""
            UPDATE siteuserdata 
            SET userid_new = CAST(userid AS VARCHAR)
            WHERE userid IS NOT NULL
        """))
        
        # 3. 删除旧列
        connection.execute(sa.text("""
            ALTER TABLE siteuserdata 
            DROP COLUMN userid
        """))
        
        # 4. 重命名新列
        connection.execute(sa.text("""
            ALTER TABLE siteuserdata 
            RENAME COLUMN userid_new TO userid
        """))
        
        logger.info("PostgreSQL数据库userid字段迁移完成")
        
    except Exception as e:
        logger.error(f"PostgreSQL数据库userid字段迁移失败: {e}")
        raise





