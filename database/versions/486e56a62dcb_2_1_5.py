"""
2.1.5 把通知模板中未使用过滤器的季号变量替换为格式化季号变量。

版本标识： 486e56a62dcb
前置版本： 89d24811e894
创建时间： 2025-05-13 19:49:51.271319
"""
import re

from alembic import op
import sqlalchemy as sa

# Alembic 使用以下标识连接迁移版本链。
revision = '486e56a62dcb'
down_revision = '89d24811e894'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 通知展示使用格式化季号，保留原始 season 字段供重命名模板使用。
    """
    把通知模板中未使用过滤器的季号变量替换为格式化季号变量。
    """
    systemconfig = sa.table(
        "systemconfig",
        sa.column("key", sa.String()),
        sa.column("value", sa.JSON()),
    )
    connection = op.get_bind()
    key = "NotificationTemplates"
    row = connection.execute(
        sa.select(systemconfig.c.value).where(systemconfig.c.key == key)
    ).first()
    templates = row[0] if row else None
    if isinstance(templates, dict):
        _re = r'(?<={{)(?![^}]*[%|])(\s*)season(\s*)(?=}})|(?<={%)if\s+(?![^%]*[%|])season\s*(?=%)'
        changed = False
        for k, v in templates.items():
            result = re.sub(_re, r'\1season_fmt\2', v)
            if result != v:
                changed = True
            templates[k] = result
        if changed:
            connection.execute(
                systemconfig.update().where(systemconfig.c.key == key).values(
                    value=templates
                )
            )


def downgrade() -> None:
    """
    本版本不执行回退操作，保留升级后的结构与数据。
    """
    pass
