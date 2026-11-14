"""
数据库迁移：${message}

版本标识： ${up_revision}
前置版本： ${down_revision | comma,n}
创建时间： ${create_date}

"""
from alembic import op
import sqlalchemy as sa
${imports if imports else ""}

# Alembic 使用以下标识连接迁移版本链。
revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}
branch_labels = ${repr(branch_labels)}
depends_on = ${repr(depends_on)}


def upgrade() -> None:
    """
    应用本版本的数据库结构或数据变更。
    """
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    """
    执行本版本定义的回退操作。
    """
    ${downgrades if downgrades else "pass"}
